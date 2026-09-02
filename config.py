"""
config.py — все настройки в одном месте.

Здесь мы читаем переменные окружения (из файла .env или из системы)
и складываем их в один объект Config. Никаких паролей в коде нет:
скрипт просто берёт то, что лежит в .env.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Папка, в которой лежит сам скрипт. Все рабочие файлы (токен, состояние,
# лог, кеш товаров) кладём рядом со скриптом, а не в текущую папку терминала,
# чтобы планировщик задач мог запускать скрипт откуда угодно.
BASE_DIR = Path(__file__).resolve().parent

# Загружаем .env. override=False означает: если переменная уже задана
# в системе, значение из .env её не перезатрёт.
load_dotenv(BASE_DIR / ".env", override=False)


def _env_str(name: str, default: str = "") -> str:
    """Читаем строку из окружения, обрезая случайные пробелы по краям."""
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    """Читаем целое число. Если в .env мусор — используем значение по умолчанию."""
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Читаем булево значение. Понимаем 1/true/yes/on в любом регистре."""
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on", "да")


class ConfigError(Exception):
    """Ошибка конфигурации: не хватает обязательной переменной в .env."""


@dataclass
class Config:
    # --- Доступ к Wipon (Prosklad) ---
    api_url: str = field(default_factory=lambda: _env_str("WIPON_API_URL", "https://api.wipon.kz").rstrip("/"))
    username: str = field(default_factory=lambda: _env_str("WIPON_USERNAME"))
    password: str = field(default_factory=lambda: _env_str("WIPON_PASSWORD"))
    employee_id: str = field(default_factory=lambda: _env_str("WIPON_EMPLOYEE_ID"))

    # Необязательный фильтр по складу. Если пусто — берём продажи по всем складам.
    stock_id: str = field(default_factory=lambda: _env_str("WIPON_STOCK_ID"))

    # Формат дат для фильтров date_from / date_to.
    # В документации тип параметра указан как "date", пример значения не приведён,
    # поэтому формат вынесен в настройку: если API ждёт дату со временем,
    # достаточно поменять строку в .env, не трогая код.
    date_format: str = field(default_factory=lambda: _env_str("WIPON_DATE_FORMAT", "%Y-%m-%d"))

    # Сколько записей просить у API за одну страницу.
    per_page: int = field(default_factory=lambda: _env_int("WIPON_PER_PAGE", 100))

    # Предохранитель от бесконечной пагинации, если API вдруг зациклится.
    max_pages: int = field(default_factory=lambda: _env_int("WIPON_MAX_PAGES", 200))

    # Таймаут одного HTTP-запроса в секундах и число повторов при сетевой ошибке.
    http_timeout: int = field(default_factory=lambda: _env_int("HTTP_TIMEOUT", 30))
    http_retries: int = field(default_factory=lambda: _env_int("HTTP_RETRIES", 3))

    # --- Google Таблица ---
    google_credentials_file: str = field(default_factory=lambda: _env_str("GOOGLE_CREDENTIALS_FILE", "service_account.json"))
    spreadsheet_id: str = field(default_factory=lambda: _env_str("GOOGLE_SPREADSHEET_ID"))
    sheet_name: str = field(default_factory=lambda: _env_str("GOOGLE_SHEET_NAME", "Продажи"))

    # Лист со сводкой по филиалам. Перезаписывается при каждом запуске.
    # Пустое значение означает «сводку не вести»: итоги и так есть
    # в строке ИТОГО каждого блока.
    summary_sheet_name: str = field(
        default_factory=lambda: _env_str("GOOGLE_SUMMARY_SHEET_NAME")
    )

    # --- Фильтр товаров ---
    # Список названий через точку с запятой: в таблицу попадут только они.
    # Пустая строка означает «писать все товары».
    # Пример: ITEM_FILTER=Магний 3x Nurelum;Омега 1000 мг 100 капсул
    item_filter: tuple = field(
        default_factory=lambda: tuple(
            name.strip()
            for name in _env_str("ITEM_FILTER").split(";")
            if name.strip()
        )
    )

    # --- Логика синхронизации ---
    # За сколько дней назад тянуть продажи при самом первом запуске
    # (когда файла состояния ещё нет).
    days_back: int = field(default_factory=lambda: _env_int("SYNC_DAYS_BACK", 3))

    # На сколько дней "перекрывать" прошлый запуск. Нужно потому, что фильтр
    # date_from работает по датам, а продажа могла появиться уже после того,
    # как мы прочитали этот день. Дубликаты всё равно отсекаются по ID продажи.
    overlap_days: int = field(default_factory=lambda: _env_int("SYNC_OVERLAP_DAYS", 1))

    # Сколько часов жить кешу товаров, прежде чем перезапросить его у API.
    items_cache_ttl_hours: int = field(default_factory=lambda: _env_int("ITEMS_CACHE_TTL_HOURS", 24))

    # Сколько ID уже записанных продаж хранить в state.json,
    # чтобы файл не рос бесконечно.
    max_remembered_ids: int = field(default_factory=lambda: _env_int("MAX_REMEMBERED_IDS", 10000))

    # Перед записью дополнительно вычитывать колонку "ID продажи" из таблицы.
    # Это страховка: даже если state.json потеряется, дубликатов не будет.
    reconcile_with_sheet: bool = field(default_factory=lambda: _env_bool("RECONCILE_WITH_SHEET", True))

    # Часовой пояс магазина. Влияет на границы дат и на то,
    # какое время продажи попадёт в таблицу.
    timezone: str = field(default_factory=lambda: _env_str("TIMEZONE", "Asia/Almaty"))

    # --- Логи и служебные файлы ---
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())
    log_file: Path = field(default_factory=lambda: BASE_DIR / _env_str("LOG_FILE", "sync.log"))
    token_file: Path = field(default_factory=lambda: BASE_DIR / "token.json")
    state_file: Path = field(default_factory=lambda: BASE_DIR / "state.json")
    items_cache_file: Path = field(default_factory=lambda: BASE_DIR / "items_cache.json")
    raw_sample_file: Path = field(default_factory=lambda: BASE_DIR / "raw_sale_sample.json")

    def credentials_path(self) -> Path:
        """Путь к JSON-ключу сервисного аккаунта Google.

        Разрешаем относительный путь: он считается относительно папки скрипта.
        """
        p = Path(self.google_credentials_file).expanduser()
        return p if p.is_absolute() else (BASE_DIR / p)

    def validate_wipon(self) -> None:
        """Проверяем, что заданы доступы к Wipon (не нужно в тестовом режиме)."""
        missing = [
            name
            for name, value in (
                ("WIPON_USERNAME", self.username),
                ("WIPON_PASSWORD", self.password),
                ("WIPON_EMPLOYEE_ID", self.employee_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "В .env не заданы обязательные переменные: " + ", ".join(missing)
            )

    def validate_google(self) -> None:
        """Проверяем, что заданы доступы к Google Таблице."""
        if not self.spreadsheet_id:
            raise ConfigError("В .env не задана переменная GOOGLE_SPREADSHEET_ID")
        cred = self.credentials_path()
        if not cred.is_file():
            raise ConfigError(
                f"JSON-ключ сервисного аккаунта не найден: {cred}. "
                "Проверьте переменную GOOGLE_CREDENTIALS_FILE в .env"
            )
