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
from typing import Any, Sequence

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import Config

log = logging.getLogger(__name__)

# Заголовки колонок — ровно в том порядке, в котором формируются строки.
HEADER = [
    "Дата и время продажи",
    "Название товара",
    "Количество",
    "Цена за единицу",
    "Сумма",
    "ID продажи",
]

# Разрешение только на работу с таблицами.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Колонка F — «ID продажи». По ней проверяем, что уже записано.
SALE_ID_COLUMN = "F"


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

    def _range(self, a1: str) -> str:
        """Собираем полный диапазон вида 'Продажи'!A1:F1."""
        return f"{_quote_sheet_name(self.sheet_name)}!{a1}"

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
            .get(spreadsheetId=self.spreadsheet_id, range=self._range("A1:F1"))
            .execute()
            .get("values", [])
        )
        if not first_row or not any(str(c).strip() for c in first_row[0]):
            log.info("Записываю строку заголовков")
            self.sheets.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=self._range("A1:F1"),
                valueInputOption="USER_ENTERED",
                body={"values": [HEADER]},
            ).execute()

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
            # F — «ID продажи», строго текстом
            column_format(5, 6, {"type": "TEXT"}),
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
                    range=self._range("A:F"),
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
        return int(updated)
