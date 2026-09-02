"""
transform.py — превращаем JSON одной продажи в строки для таблицы.

Одна продажа (чек) может содержать несколько товаров, поэтому из одной
продажи получается несколько строк:

    Дата и время продажи | Название товара | Количество | Цена за единицу | Сумма | ID продажи

Важное замечание про документацию:
в разделе «Просмотр списка продаж» описаны только параметры запроса,
примера ответа там нет. Поэтому имена полей мы берём из тех разделов,
где они задокументированы:
  * id, title, barcode, created_at  — раздел «Получение списка товаров»;
  * item_id, quantity, selling_price — раздел «Создание продажи» (v2);
  * item_sale_id, quick_title        — раздел «Создание возврата».
Ничего сверх этого не выдумываем: если поле называется иначе,
скрипт напишет предупреждение в лог и сохранит сырой JSON продажи
в файл raw_sale_sample.json, чтобы можно было сверить названия.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Список полей-кандидатов для даты продажи. created_at — единственное
# поле даты, задокументированное в примерах ответов; остальные проверяем
# как запасной вариант, если структура ответа окажется иной.
DATE_FIELDS = ("created_at", "date", "sale_date", "closed_at", "updated_at")

# Где может лежать список товаров внутри продажи.
# item_sale — это реальное имя поля, проверенное на боевом ответе API;
# остальные оставлены как запасные варианты на случай смены версии API.
ITEMS_FIELDS = ("item_sale", "items", "item_sales", "sale_items")

# Где может лежать название товара внутри позиции чека.
TITLE_FIELDS = ("title", "quick_title", "item_title", "name")

# Где искать филиал (точку продажи). В боевом ответе это объект stock:
#   "stock": {"id": 69602, "name": "2 точка", ...}
# Если его вдруг не окажется, пробуем кассу — она тоже привязана к точке.
BRANCH_SOURCES = ("stock", "cashbox")


@dataclass
class SaleRow:
    """Одна строка будущей таблицы."""

    sold_at: str        # Дата и время продажи (строка, уже в нужном часовом поясе)
    branch: str         # Филиал (точка продажи)
    title: str          # Название товара
    quantity: float     # Количество
    unit_price: float   # Цена за единицу
    total: float        # Сумма = количество * цена
    sale_id: str        # ID продажи (по нему отсекаем дубликаты)

    def as_block_row(self) -> List[Any]:
        """Строка для блока филиала: колонка «Филиал» там не нужна —
        весь блок и так про одну точку."""
        return [
            self.sold_at,
            self.title,
            self.quantity,
            self.unit_price,
            self.total,
            self.sale_id,
        ]

    def as_sheet_row(self) -> List[Any]:
        """Порядок значений строго как порядок колонок в таблице."""
        return [
            self.sold_at,
            self.branch,
            self.title,
            self.quantity,
            self.unit_price,
            self.total,
            self.sale_id,
        ]


def _to_float(value: Any) -> float:
    """Приводим значение к числу.

    API отдаёт числа строками: "10.000", "20.00". Иногда встречается
    запятая вместо точки — обрабатываем и такой случай.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return 0.0


def _first_present(source: Mapping[str, Any], keys: Sequence[str]) -> Optional[Any]:
    """Возвращаем первое непустое значение из перечисленных ключей."""
    for key in keys:
        value = source.get(key)
        if value not in (None, "", []):
            return value
    return None


def format_datetime(raw: Any, tz: ZoneInfo) -> str:
    """Приводим дату из API к виду «ДД.ММ.ГГГГ ЧЧ:ММ:СС» в часовом поясе магазина.

    В документации даты приходят в ISO8601 с зоной: "2025-03-13T16:59:08+05:00".
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # Python не понимает суффикс "Z", заменяем его на "+00:00".
    normalized = text.replace("Z", "+00:00")
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(normalized)
        except ValueError:
            continue
        # Если в дате не было часового пояса — считаем, что она уже местная.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")

    # Формат неизвестен — не теряем данные, кладём как есть.
    log.warning("Не смог разобрать дату продажи: %r", text)
    return text


def _resolve_title(position: Mapping[str, Any], item_titles: Mapping[str, str]) -> str:
    """Определяем название товара для позиции чека.

    Порядок поиска:
      1. Название прямо в позиции чека (title / quick_title).
      2. Вложенный объект item -> title.
      3. Кеш справочника товаров по item_id.
      4. Кеш справочника товаров по штрихкоду.
      5. Заглушка с ID, чтобы строка всё равно попала в таблицу.
    """
    direct = _first_present(position, TITLE_FIELDS)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    nested = position.get("item")
    if isinstance(nested, dict):
        nested_title = _first_present(nested, TITLE_FIELDS)
        if isinstance(nested_title, str) and nested_title.strip():
            return nested_title.strip()

    item_id = _first_present(position, ("item_id", "id"))
    if isinstance(nested, dict) and nested.get("id") is not None:
        item_id = nested["id"]
    if item_id is not None:
        found = item_titles.get(f"id:{item_id}")
        if found:
            return found

    barcode = _first_present(position, ("barcode", "quick_barcode"))
    if barcode:
        found = item_titles.get(f"barcode:{barcode}")
        if found:
            return found

    return f"Товар #{item_id}" if item_id is not None else "Товар без названия"


def resolve_branch(sale: Mapping[str, Any]) -> str:
    """Достаём название филиала из продажи.

    В ответе API филиал лежит в объекте stock («склад» в терминах Wipon,
    но фактически это торговая точка): stock.name = «2 точка».
    """
    for source in BRANCH_SOURCES:
        nested = sale.get(source)
        if isinstance(nested, dict):
            name = nested.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return "Без филиала"


def sale_to_rows(
    sale: Mapping[str, Any],
    item_titles: Mapping[str, str],
    tz: ZoneInfo,
    item_filter: Sequence[str] = (),
) -> List[SaleRow]:
    """Разбираем одну продажу на строки таблицы (по строке на товар).

    item_filter — список названий товаров, которые нас интересуют.
    Пустой список означает «берём все товары».
    """
    sale_id = sale.get("id")
    if sale_id is None:
        log.warning("Пропускаю продажу без поля id: %.200s", sale)
        return []

    sold_at = format_datetime(_first_present(sale, DATE_FIELDS), tz)
    branch = resolve_branch(sale)

    positions = _first_present(sale, ITEMS_FIELDS)
    if not isinstance(positions, list) or not positions:
        log.warning("В продаже id=%s нет списка товаров — строка не создана", sale_id)
        return []

    rows: List[SaleRow] = []
    for position in positions:
        if not isinstance(position, dict):
            continue

        title = _resolve_title(position, item_titles)

        # Фильтр по названию товара. Сравниваем без учёта регистра и
        # лишних пробелов, чтобы «магний 3x nurelum» тоже находился.
        if item_filter and title.casefold().strip() not in {
            wanted.casefold().strip() for wanted in item_filter
        }:
            continue

        quantity = _to_float(_first_present(position, ("quantity",)))
        unit_price = _to_float(_first_present(position, ("selling_price", "price")))

        # Сумму считаем сами: количество * цена, готового поля суммы у позиции нет.
        # Проверено на боевом чеке: selling_price — это цена за единицу УЖЕ со
        # скидкой, а price — цена до скидки. Поэтому сумма всех строк чека
        # (quantity * selling_price) точно совпадает с полем sum самой продажи.
        total = round(quantity * unit_price, 2)

        rows.append(
            SaleRow(
                sold_at=sold_at,
                branch=branch,
                title=title,
                quantity=quantity,
                unit_price=unit_price,
                total=total,
                sale_id=str(sale_id),
            )
        )

    return rows


def sales_to_rows(
    sales: Sequence[Mapping[str, Any]],
    item_titles: Mapping[str, str],
    tz: ZoneInfo,
    item_filter: Sequence[str] = (),
) -> List[SaleRow]:
    """Разбираем список продаж, сортируя итог по дате (старые продажи выше)."""
    rows: List[SaleRow] = []
    for sale in sales:
        rows.extend(sale_to_rows(sale, item_titles, tz, item_filter))
    return rows


def branch_sort_key(branch: str) -> tuple:
    """Ключ сортировки филиалов — всегда один и тот же порядок.

    Обычная сортировка по алфавиту поставила бы «2 точка» и «3 точка»
    впереди «Основного склада», потому что цифры идут раньше букв.
    Поэтому сортируем «естественно»:
      1. филиалы без номера в названии (Основной склад) — первыми;
      2. остальные — по возрастанию номера: 2 точка, 3 точка, 10 точка.

    Порядок не зависит от того, где больше продали, поэтому строки
    в сводке всегда стоят на своих местах и их удобно сравнивать
    день ото дня.
    """
    match = re.search(r"\d+", branch)
    if match:
        return (1, int(match.group()), branch.casefold())
    return (0, 0, branch.casefold())


def build_branch_summary(by_branch: Mapping[str, Sequence[Sequence[Any]]]) -> dict:
    """Считаем итоги по филиалам на основе блоков, прочитанных из таблицы.

    Строка блока устроена так:
        [дата, товар, количество, цена, сумма, ID продажи]

    Возвращаем {"2 точка": {"quantity": 27.0, "total": 317358.02, "receipts": 24}}.
    Чеки считаем по уникальным ID: в одном чеке товар может встретиться
    несколькими строками, но чек при этом один.
    """
    summary: dict = {}
    for branch, rows in by_branch.items():
        quantity = total = 0.0
        receipts = set()
        for row in rows:
            if len(row) < 5:
                continue
            quantity += _to_float(row[2])
            total += _to_float(row[4])
            sale_id = str(row[5]).strip() if len(row) > 5 else ""
            if sale_id:
                receipts.add(sale_id)
        summary[branch] = {
            "quantity": round(quantity, 3),
            "total": round(total, 2),
            "receipts": len(receipts),
        }
    return summary


def sale_row_sort_key(row: Sequence[Any]) -> tuple:
    """Порядок строк внутри блока — хронологический.

    Сортируем по ID продажи: он выдаётся кассой по возрастанию, поэтому
    работает как надёжная замена дате. Саму дату для этого использовать
    неудобно: у прочитанных из таблицы строк она приходит числом Google,
    а у новых — строкой «03.09.2026 01:20:00», и сравнивать их пришлось бы
    через разбор формата.
    """
    sale_id = str(row[5]).strip() if len(row) > 5 else ""
    try:
        return (0, int(sale_id), "")
    except ValueError:
        return (1, 0, sale_id)


def sale_sort_key(sale: Mapping[str, Any]) -> tuple:
    """Ключ сортировки продаж: сначала по дате, потом по id.

    Нужен, чтобы строки ложились в таблицу в хронологическом порядке.
    """
    raw_date = _first_present(sale, DATE_FIELDS) or ""
    try:
        numeric_id = int(sale.get("id"))
    except (TypeError, ValueError):
        numeric_id = 0
    return (str(raw_date), numeric_id)
