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

log = logging.getLogger(__name__)

# Заголовки колонок — ровно в том порядке, в котором формируются строки.
HEADER = [
    "Дата и время продажи",
    "Филиал",
    "Название товара",
    "Количество",
    "Цена за единицу",
    "Сумма",
    "ID продажи",
]

# Разрешение только на работу с таблицами.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Колонка G — «ID продажи». По ней проверяем, что уже записано.
# (была F, сдвинулась вправо после добавления колонки «Филиал»)
SALE_ID_COLUMN = "G"

# Последняя колонка таблицы — нужна для диапазонов вида A1:G1.
LAST_COLUMN = "G"


class SheetsError(Exception):
    """Ошибка при работе с Google Sheets."""


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

    def ensure_sheet_and_header(self) -> None:
        """Проверяем, что лист существует и первая строка — заголовки."""
        try:
            meta = self.sheets.get(spreadsheetId=self.spreadsheet_id).execute()
        except HttpError as exc:
            raise SheetsError(
                f"Не удалось открыть таблицу {self.spreadsheet_id}. "
                "Проверьте GOOGLE_SPREADSHEET_ID и доступ сервисного аккаунта. "
                f"Ответ Google: {exc}"
            ) from exc

        # Ищем нужный лист среди существующих и заодно запоминаем его
        # числовой sheetId — он понадобится, чтобы задать формат колонок.
        sheet_id = None
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]
            if props["title"] == self.sheet_name:
                sheet_id = props["sheetId"]
                break

        if sheet_id is None:
            log.info("Лист «%s» не найден — создаю", self.sheet_name)
            response = self.sheets.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": self.sheet_name}}}]},
            ).execute()
            sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]

        # Читаем первую строку. Если пусто — записываем заголовки.
        first_row = (
            self.sheets.values()
            .get(spreadsheetId=self.spreadsheet_id, range=self._range(f"A1:{LAST_COLUMN}1"))
            .execute()
            .get("values", [])
        )
        if not first_row or not any(str(c).strip() for c in first_row[0]):
            log.info("Записываю строку заголовков")
            self.sheets.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=self._range(f"A1:{LAST_COLUMN}1"),
                valueInputOption="USER_ENTERED",
                body={"values": [HEADER]},
            ).execute()

        self._sheet_id = sheet_id
        self._apply_column_formats(sheet_id)

    def _apply_column_formats(self, sheet_id: int) -> None:
        """Задаём формат колонок, чтобы Google не искажал наши данные.

        Зачем это нужно: мы пишем с valueInputOption=USER_ENTERED, то есть
        Google сам разбирает, что мы прислали. Строку «02.09.2026 21:14:22»
        он справедливо распознаёт как дату-время и хранит её числом
        (у Google дата — это количество дней с 30.12.1899). Если у ячейки
        при этом числовой формат, в таблице видно «46264,50641» вместо даты.
        Поэтому один раз явно объявляем: колонка A — дата-время,
        колонка F (ID продажи) — текст, чтобы длинные ID не превращались
        в экспоненциальную запись вида 3,4379E+08.
        """
        def column_format(start: int, end: int, number_format: dict) -> dict:
            return {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,  # строку 1 не трогаем — это заголовки
                        "startColumnIndex": start,
                        "endColumnIndex": end,
                    },
                    "cell": {"userEnteredFormat": {"numberFormat": number_format}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }

        requests = [
            # A — «Дата и время продажи»
            column_format(0, 1, {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm:ss"}),
            # G — «ID продажи», строго текстом
            column_format(6, 7, {"type": "TEXT"}),
        ]
        try:
            self.sheets.batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            # Формат — это косметика: если не вышло, данные всё равно запишутся.
            log.warning("Не удалось задать формат колонок (%s) — продолжаю", exc)

    # ------------------------------------------------------------------
    # Чтение уже записанного и дозапись
    # ------------------------------------------------------------------

    def existing_sale_ids(self) -> set:
        """Читаем колонку «ID продажи» целиком.

        Это вторая линия защиты от дублей: даже если файл state.json
        удалили или потеряли, скрипт увидит, какие продажи уже в таблице.
        """
        try:
            values = (
                self.sheets.values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=self._range(f"{SALE_ID_COLUMN}2:{SALE_ID_COLUMN}"),
                )
                .execute()
                .get("values", [])
            )
        except HttpError as exc:
            log.warning("Не удалось прочитать ID из таблицы (%s) — полагаюсь на state.json", exc)
            return set()

        ids = {str(row[0]).strip() for row in values if row and str(row[0]).strip()}
        log.info("В таблице уже записано продаж: %d", len(ids))
        return ids

    def read_data_rows(self) -> List[List[Any]]:
        """Читаем все строки листа продаж без шапки.

        valueRenderOption=UNFORMATTED_VALUE — просим Google вернуть «сырые»
        значения: числа числами, а не строкой «1 234,50», иначе их пришлось
        бы разбирать обратно с учётом того, какая в таблице локаль.
        """
        try:
            values = (
                self.sheets.values()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    range=self._range(f"A2:{LAST_COLUMN}"),
                    valueRenderOption="UNFORMATTED_VALUE",
                )
                .execute()
                .get("values", [])
            )
        except HttpError as exc:
            raise SheetsError(f"Не удалось прочитать таблицу: {exc}") from exc
        return values

    def append_rows(self, rows: Sequence[Sequence[Any]]) -> int:
        """Дописываем строки в конец листа одним запросом.

        valueInputOption=USER_ENTERED — Google сам распознает числа и даты,
        так что количество и суммы будут именно числами, а не текстом.
        insertDataOption=INSERT_ROWS — вставка новых строк, а не перезапись
        того, что может лежать ниже таблицы.
        """
        if not rows:
            return 0
        try:
            response = (
                self.sheets.values()
                .append(
                    spreadsheetId=self.spreadsheet_id,
                    range=self._range(f"A:{LAST_COLUMN}"),
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [list(r) for r in rows]},
                )
                .execute()
            )
        except HttpError as exc:
            raise SheetsError(f"Не удалось записать строки в таблицу: {exc}") from exc

        updated = response.get("updates", {}).get("updatedRows", len(rows))
        log.info("В таблицу дописано строк: %s", updated)

        # Формат колонок задаём именно здесь, а не только при создании листа:
        # append дописывает строки НИЖЕ той области, к которой формат уже
        # применялся, и свежие ячейки достаются без него — тогда дата
        # показывается числом вида 46238,5587 вместо 30.08.2026 13:24:31.
        if self._sheet_id is not None:
            self._apply_column_formats(self._sheet_id)

        return int(updated)

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

        # Филиалы по убыванию количества: самый продающий сверху.
        for branch, data in sorted(
            by_branch.items(), key=lambda kv: kv[1]["quantity"], reverse=True
        ):
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
