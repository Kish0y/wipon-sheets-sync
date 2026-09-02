"""
sheets.py — запись строк в Google Таблицу через service account.

Что делает класс SheetsWriter:
  1. Авторизуется JSON-ключом сервисного аккаунта.
  2. Создаёт лист (вкладку), если его ещё нет.
  3. Ставит строку заголовков, если таблица пустая.
  4. Читает уже записанные ID продаж (страховка от дублей).
  5. Дописывает новые строки в конец.

Не забудьте выдать сервисному аккаунту доступ «Редактор» к таблице:
откройте таблицу -> «Настройки доступа» -> добавьте email вида
имя@проект.iam.gserviceaccount.com (он лежит в JSON-ключе, поле client_email).
"""

import logging
from typing import Any, List, Mapping, Sequence

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import Config
from transform import branch_sort_key

log = logging.getLogger(__name__)

# Заголовки колонок — ровно в том порядке, в котором формируются строки.
BRANCH_HEADER = [
    "Дата и время продажи",
    "Название товара",
    "Количество",
    "Цена за единицу",
    "Сумма",
    "ID продажи",
]

# --- Раскладка блоков по филиалам ---
# Каждый филиал занимает свой блок колонок, блоки идут слева направо:
#   A..F  — первый филиал,  G и H — пустые (разделитель)
#   I..N  — второй филиал,  O и P — пустые
#   Q..V  — третий филиал,  W     — пустая
# Данных в блоке шесть колонок (BLOCK_WIDTH), а шаг между началами
# блоков — восемь (BLOCK_STEP), поэтому между ними остаётся зазор.
BLOCK_WIDTH = 6
BLOCK_STEP = 8

# Разрешение только на работу с таблицами.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsError(Exception):
    """Ошибка при работе с Google Sheets."""


def column_letter(index: int) -> str:
    """Номер колонки (0 = A) в её буквенное обозначение: 0→A, 7→H, 26→AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def _to_number(value: Any) -> float:
    """Приводим значение из таблицы к числу.

    Google при valueRenderOption=UNFORMATTED_VALUE отдаёт числа числами,
    но в ячейке может оказаться и текст — тогда считаем её нулём.
    """
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0


def _quote_sheet_name(name: str) -> str:
    """Оборачиваем имя листа в кавычки для A1-нотации.

    Нужно, потому что имя может содержать пробелы или кириллицу
    («Продажи», «Отчёт за месяц»). Одинарная кавычка внутри имени удваивается.
    """
    return "'" + name.replace("'", "''") + "'"


class SheetsWriter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.validate_google()

        # Авторизация сервисного аккаунта: библиотека сама обменяет
        # приватный ключ из JSON-файла на токен доступа Google.
        credentials = Credentials.from_service_account_file(
            str(cfg.credentials_path()), scopes=SCOPES
        )
        # cache_discovery=False убирает предупреждение о недоступном кеше.
        self.service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.sheets = self.service.spreadsheets()
        self.spreadsheet_id = cfg.spreadsheet_id
        self.sheet_name = cfg.sheet_name
        self.summary_sheet_name = cfg.summary_sheet_name
        # sheetId основного листа. Заполняется в ensure_sheet_and_header
        # и нужен, чтобы задать формат колонок после дозаписи строк.
        self._sheet_id = None

    def _range(self, a1: str, sheet: str = "") -> str:
        """Собираем полный диапазон вида 'Продажи'!A1:G1.

        sheet — имя листа; по умолчанию основной лист с продажами.
        """
        return f"{_quote_sheet_name(sheet or self.sheet_name)}!{a1}"

    def _ensure_sheet_exists(self, title: str) -> int:
        """Создаём лист, если его ещё нет. Возвращаем его числовой sheetId."""
        meta = self.sheets.get(spreadsheetId=self.spreadsheet_id).execute()
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]
            if props["title"] == title:
                return props["sheetId"]

        log.info("Лист «%s» не найден — создаю", title)
        response = self.sheets.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        return response["replies"][0]["addSheet"]["properties"]["sheetId"]

    # ------------------------------------------------------------------
    # Подготовка таблицы
    # ------------------------------------------------------------------

    def ensure_sheet(self) -> None:
        """Проверяем, что лист существует, и запоминаем его sheetId.

        Строку заголовков здесь не пишем: в новой раскладке первая строка
        занята названиями филиалов, а шапки идут внутри каждого блока —
        всё это формируется при записи блоков.
        """
        try:
            self.sheets.get(spreadsheetId=self.spreadsheet_id).execute()
        except HttpError as exc:
            raise SheetsError(
                f"Не удалось открыть таблицу {self.spreadsheet_id}. "
                "Проверьте GOOGLE_SPREADSHEET_ID и доступ сервисного аккаунта. "
                f"Ответ Google: {exc}"
            ) from exc
        self._sheet_id = self._ensure_sheet_exists(self.sheet_name)

    def read_branch_blocks(self) -> dict:
        """Читаем лист и разбираем его обратно на блоки по филиалам.

        Лист — это первоисточник: скрипт не хранит продажи у себя, поэтому
        перед перезаписью он вычитывает то, что уже записано, и дополняет
        новыми строками.

        Возвращаем {"2 точка": [[дата, товар, кол-во, цена, сумма, ID], ...]}.
        Строку с названием филиала, шапку и ИТОГО в данные не берём.
        """
        last = column_letter(2 * BLOCK_STEP + BLOCK_WIDTH + 1)
        try:
            values = (
                self.sheets.values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=self._range(f"A:{last}"),
                    valueRenderOption="UNFORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
        except HttpError as exc:
            log.warning("Не удалось прочитать таблицу (%s) — считаю её пустой", exc)
            return {}

        if not values:
            return {}

        by_branch: dict = {}
        # Блоков может быть больше трёх, если в кассе появится новый филиал.
        block_count = (max(len(row) for row in values) + BLOCK_STEP - 1) // BLOCK_STEP
        for index in range(max(block_count, 1)):
            start_col = index * BLOCK_STEP

            def cell(row_index: int, offset: int = 0):
                if row_index >= len(values):
                    return ""
                row = values[row_index]
                position = start_col + offset
                return row[position] if position < len(row) else ""

            branch = str(cell(0)).strip()
            if not branch:
                continue

            rows = []
            for row_index in range(2, len(values)):      # строки 1 и 2 — название и шапка
                first = str(cell(row_index)).strip()
                if not first or first.upper().startswith("ИТОГО"):
                    break
                rows.append([cell(row_index, offset) for offset in range(BLOCK_WIDTH)])
            by_branch[branch] = rows
        return by_branch

    def existing_sale_ids(self) -> set:
        """Собираем ID уже записанных продаж из всех блоков.

        Это вторая линия защиты от дублей: даже если файл state.json
        потеряли, скрипт видит по таблице, что уже записано.
        """
        ids = set()
        for rows in self.read_branch_blocks().values():
            for row in rows:
                sale_id = str(row[BLOCK_WIDTH - 1]).strip()
                if sale_id:
                    ids.add(sale_id)
        log.info("В таблице уже записано продаж: %d", len(ids))
        return ids

    def write_branch_blocks(self, by_branch: Mapping[str, Sequence[Sequence[Any]]]) -> None:
        """Записываем все блоки заново — целиком, а не дозаписью.

        Почему не append: строка ИТОГО стоит сразу под последней продажей
        своего блока, а у филиалов разное число продаж. Дозапись положила бы
        новые строки под ИТОГО и раскладка поехала бы.
        """
        branches = [b for b in sorted(by_branch, key=branch_sort_key)]
        if not branches:
            return

        blocks: List[List[List[Any]]] = []
        for branch in branches:
            rows = [list(r) for r in by_branch[branch]]
            quantity = sum(_to_number(r[2]) for r in rows if len(r) > 2)
            total = sum(_to_number(r[4]) for r in rows if len(r) > 4)

            block = [[branch], list(BRANCH_HEADER)]
            block.extend(rows)
            # Строка ИТОГО идёт сразу после последней продажи, без отступа:
            # «ИТОГО» в первой колонке, количество и сумма — в своих.
            block.append(["ИТОГО", "", quantity, "", total, ""])
            blocks.append(block)

        height = max(len(b) for b in blocks)
        width = (len(blocks) - 1) * BLOCK_STEP + BLOCK_WIDTH
        grid: List[List[Any]] = [["" for _ in range(width)] for _ in range(height)]
        for index, block in enumerate(blocks):
            start_col = index * BLOCK_STEP
            for row_index, row in enumerate(block):
                for offset, value in enumerate(row):
                    grid[row_index][start_col + offset] = value

        last = column_letter(width + 1)
        try:
            # Чистим весь диапазон: у филиала могло стать меньше продаж,
            # и от прошлого запуска остались бы хвосты.
            self.sheets.values().clear(
                spreadsheetId=self.spreadsheet_id,
                range=self._range(f"A:{last}"),
                body={},
            ).execute()
            self.sheets.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=self._range("A1"),
                valueInputOption="USER_ENTERED",
                body={"values": grid},
            ).execute()
            self._apply_block_formats(len(blocks))
        except HttpError as exc:
            raise SheetsError(f"Не удалось записать блоки филиалов: {exc}") from exc

        for branch, block in zip(branches, blocks):
            log.info("Блок «%s»: продаж %d", branch, len(block) - 3)

    def _apply_block_formats(self, block_count: int) -> None:
        """Формат колонок в каждом блоке: дата — датой, ID — текстом."""
        if self._sheet_id is None:
            return
        requests = []
        for index in range(block_count):
            start_col = index * BLOCK_STEP
            for offset, fmt in (
                (0, {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm:ss"}),
                (BLOCK_WIDTH - 1, {"type": "TEXT"}),
            ):
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": self._sheet_id,
                            "startRowIndex": 2,      # ниже названия филиала и шапки
                            "startColumnIndex": start_col + offset,
                            "endColumnIndex": start_col + offset + 1,
                        },
                        "cell": {"userEnteredFormat": {"numberFormat": fmt}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                })
        try:
            self.sheets.batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            log.warning("Не удалось задать формат колонок (%s) — продолжаю", exc)

    # ------------------------------------------------------------------
    # Отдельный лист под каждый филиал
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Сводка по филиалам
    # ------------------------------------------------------------------

    def write_summary(
        self,
        item_names: Sequence[str],
        by_branch: Mapping[str, Mapping[str, Any]],
        generated_at: str,
    ) -> None:
        """Перезаписываем лист со сводкой: по блоку на каждый филиал.

        by_branch — словарь вида
            {"2 точка": {"quantity": 10, "total": 119358, "receipts": 9}, ...}

        Лист каждый раз переписывается заново, а не дополняется: сводка
        показывает текущее состояние, накапливать в ней нечего.
        """
        self._ensure_sheet_exists(self.summary_sheet_name)

        title = ", ".join(item_names) if item_names else "все товары"
        rows: List[List[Any]] = [
            ["Сводка по товару:", title],
            ["Обновлено:", generated_at],
            [],
        ]

        total_quantity = 0.0
        total_sum = 0.0
        total_receipts = 0

        # Порядок филиалов постоянный (см. branch_sort_key), чтобы строки
        # не прыгали от запуска к запуску и цифры было удобно сравнивать.
        for branch, data in sorted(by_branch.items(), key=lambda kv: branch_sort_key(kv[0])):
            rows.append([branch])
            rows.append(["Количество, шт", data["quantity"]])
            rows.append(["Сумма, тг", data["total"]])
            rows.append(["Чеков", data["receipts"]])
            rows.append([])          # пустая строка между филиалами
            total_quantity += data["quantity"]
            total_sum += data["total"]
            total_receipts += data["receipts"]

        rows.append(["ИТОГО ПО ВСЕМ ФИЛИАЛАМ"])
        rows.append(["Количество, шт", total_quantity])
        rows.append(["Сумма, тг", total_sum])
        rows.append(["Чеков", total_receipts])

        target = self._range("A:D", self.summary_sheet_name)
        try:
            # Сначала стираем старую сводку, иначе от прошлого запуска
            # могут остаться строки, если филиалов стало меньше.
            self.sheets.values().clear(
                spreadsheetId=self.spreadsheet_id, range=target, body={}
            ).execute()
            self.sheets.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=self._range("A1", self.summary_sheet_name),
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            ).execute()
        except HttpError as exc:
            raise SheetsError(f"Не удалось записать сводку: {exc}") from exc

        log.info(
            "Сводка обновлена: филиалов %d, всего %s шт на %s тг",
            len(by_branch), total_quantity, total_sum,
        )
