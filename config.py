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


# Сколько аккаунтов Wipon максимум ищем в .env (WIPON_2_..., WIPON_3_... и т. д.).
MAX_ACCOUNTS = 9


@dataclass
class WiponAccount:
    """Один аккаунт Wipon (одна касса / одна компания).

    Первый аккаунт задаётся переменными без номера:
        WIPON_USERNAME, WIPON_PASSWORD, WIPON_EMPLOYEE_ID
    Второй и следующие — с номером после WIPON_:
        WIPON_2_USERNAME, WIPON_2_PASSWORD, WIPON_2_EMPLOYEE_ID, WIPON_2_NAME

    У каждого аккаунта свои служебные файлы (токен, состояние, кеш товаров):
    токены и ID продаж у разных касс разные, смешивать их нельзя.
    """

    number: int          # 1, 2, 3...
    # Приставка к названиям филиалов в таблице. Нужна, потому что у разных
    # касс филиалы часто называются одинаково («Основной склад»), и без
    # приставки их продажи слились бы в один блок. У первого аккаунта
    # приставка по умолчанию пустая: так уже записанные блоки остаются на месте.
    label: str
    username: str
    password: str
    employee_id: str
    # Необязательный фильтр по складу. Если пусто — берём продажи по всем складам.
    stock_id: str

    @property
    def prefix(self) -> str:
        """Префикс имён переменных в .env: WIPON_ для первого, WIPON_2_ для второго."""
        return "WIPON_" if self.number == 1 else f"WIPON_{self.number}_"

    @property
    def title(self) -> str:
        """Как называть аккаунт в логах."""
        return f"Аккаунт {self.number}" + (f" «{self.label}»" if self.label else "")

    def _file(self, stem: str, suffix: str) -> Path:
        """Имя служебного файла. У первого аккаунта оно прежнее (token.json),
        у остальных с номером (token_2.json), чтобы не сломать уже работающую
        установку."""
        name = f"{stem}{suffix}" if self.number == 1 else f"{stem}_{self.number}{suffix}"
        return BASE_DIR / name

    @property
    def token_file(self) -> Path:
        return self._file("token", ".json")

    @property
    def state_file(self) -> Path:
        return self._file("state", ".json")

    @property
    def items_cache_file(self) -> Path:
        return self._file("items_cache", ".json")

    @property
    def raw_sample_file(self) -> Path:
        return self._file("raw_sale_sample", ".json")

    def validate(self) -> None:
        """Проверяем, что у аккаунта заданы логин, пароль и ID сотрудника."""
        missing = [
            self.prefix + name
            for name, value in (
                ("USERNAME", self.username),
                ("PASSWORD", self.password),
                ("EMPLOYEE_ID", self.employee_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"{self.title}: в .env не заданы переменные: " + ", ".join(missing)
            )


def _read_account(number: int) -> WiponAccount:
    """Собираем аккаунт с номером number из переменных окружения."""
    prefix = "WIPON_" if number == 1 else f"WIPON_{number}_"
    default_label = "" if number == 1 else f"Аккаунт {number}"
    return WiponAccount(
        number=number,
        label=_env_str(prefix + "NAME", default_label),
        username=_env_str(prefix + "USERNAME"),
        password=_env_str(prefix + "PASSWORD"),
        employee_id=_env_str(prefix + "EMPLOYEE_ID"),
        stock_id=_env_str(prefix + "STOCK_ID"),
    )


def load_accounts() -> list:
    """Все аккаунты, которые заданы в .env, по порядку номеров.

    Первый аккаунт есть всегда (если логин не заполнен, это выяснится при
    проверке). Второй и следующие считаются заданными, если у них заполнен
    логин: WIPON_2_USERNAME, WIPON_3_USERNAME...
    """
    accounts = [_read_account(1)]
    for number in range(2, MAX_ACCOUNTS + 1):
        if _env_str(f"WIPON_{number}_USERNAME"):
            accounts.append(_read_account(number))

    # Две кассы с одной приставкой снова смешали бы филиалы — не допускаем.
    labels = [a.label for a in accounts]
    duplicates = {label for label in labels if labels.count(label) > 1}
    if duplicates:
        raise ConfigError(
            "У аккаунтов совпадают названия (переменные WIPON_NAME / WIPON_N_NAME): "
            + ", ".join(repr(d) for d in sorted(duplicates))
        )
    return accounts


@dataclass
class Config:
    # --- Доступ к Wipon (Prosklad) ---
    api_url: str = field(default_factory=lambda: _env_str("WIPON_API_URL", "https://api.wipon.kz").rstrip("/"))

    # Аккаунты Wipon: логины, пароли и ID сотрудников. Подробнее — WiponAccount.
    accounts: list = field(default_factory=load_accounts)

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

    # --- Логи ---
    # Служебные файлы аккаунтов (токен, состояние, кеш) описаны в WiponAccount.
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())
    log_file: Path = field(default_factory=lambda: BASE_DIR / _env_str("LOG_FILE", "sync.log"))

    def credentials_path(self) -> Path:
        """Путь к JSON-ключу сервисного аккаунта Google.

        Разрешаем относительный путь: он считается относительно папки скрипта.
        """
        p = Path(self.google_credentials_file).expanduser()
        return p if p.is_absolute() else (BASE_DIR / p)

    def account_labels(self) -> list:
        """Приставки аккаунтов по порядку — задают порядок блоков на листе."""
        return [a.label for a in self.accounts]

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
