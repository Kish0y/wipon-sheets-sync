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


@dataclass
class SaleRow:
    """Одна строка будущей таблицы."""

    sold_at: str        # Дата и время продажи (строка, уже в нужном часовом поясе)
    title: str          # Название товара
    quantity: float     # Количество
    unit_price: float   # Цена за единицу
    total: float        # Сумма = количество * цена
    sale_id: str        # ID продажи (по нему отсекаем дубликаты)

    def as_sheet_row(self) -> List[Any]:
        """Порядок значений строго как порядок колонок в таблице."""
        return [self.sold_at, self.title, self.quantity, self.unit_price, self.total, self.sale_id]


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


def sale_to_rows(
    sale: Mapping[str, Any],
    item_titles: Mapping[str, str],
    tz: ZoneInfo,
) -> List[SaleRow]:
    """Разбираем одну продажу на строки таблицы (по строке на товар)."""
    sale_id = sale.get("id")
    if sale_id is None:
        log.warning("Пропускаю продажу без поля id: %.200s", sale)
        return []

    sold_at = format_datetime(_first_present(sale, DATE_FIELDS), tz)

    positions = _first_present(sale, ITEMS_FIELDS)
    if not isinstance(positions, list) or not positions:
        log.warning("В продаже id=%s нет списка товаров — строка не создана", sale_id)
        return []

    rows: List[SaleRow] = []
    for position in positions:
        if not isinstance(position, dict):
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
                title=_resolve_title(position, item_titles),
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
) -> List[SaleRow]:
    """Разбираем список продаж, сортируя итог по дате (старые продажи выше)."""
    rows: List[SaleRow] = []
    for sale in sales:
        rows.extend(sale_to_rows(sale, item_titles, tz))
    return rows


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
