#!/usr/bin/env python
"""
wipon_sync.py — главный файл. Переносит продажи из кассы Wipon в Google Таблицу.

Как это работает за один запуск (для каждого аккаунта Wipon из .env):
  1. Читаем настройки из .env и вспоминаем, что уже синхронизировали (state.json).
  2. Берём access_token: из файла token.json, а если его нет или он протух —
     запрашиваем новый через POST /v1/oauth/token.
  3. Скачиваем продажи за период GET /v1/employee/{employee_id}/sale.
  4. Подтягиваем названия товаров из GET /v2/employee/{employee_id}/item (с кешем).
  5. Отбрасываем продажи, которые уже записаны (по ID продажи).
  6. Дописываем оставшиеся строки в Google Таблицу.
  7. Сохраняем состояние и пишем в лог итог: сколько строк добавлено.

Скрипт рассчитан на запуск по расписанию: он отрабатывает один раз и выходит,
никаких бесконечных циклов внутри нет.

Примеры запуска:
    python wipon_sync.py                 # обычная синхронизация
    python wipon_sync.py --test          # фейковые данные, реальная запись в таблицу
    python wipon_sync.py --test --dry-run  # ничего не пишем, только показываем строки
    python wipon_sync.py --days 7        # разово забрать продажи за 7 дней
    python wipon_sync.py --reset-state   # забыть, что уже синхронизировано
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any, List, Mapping, Sequence, Set
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import Config, ConfigError, WiponAccount
from state import SyncState
from transform import (
    build_branch_summary,
    make_block_sort_key,
    sale_row_sort_key,
    SaleRow,
    sale_sort_key,
    sales_to_rows,
)

log = logging.getLogger("wipon_sync")


# ----------------------------------------------------------------------
# БЛОК 1. Логирование
# ----------------------------------------------------------------------

class ConsoleFormatter(logging.Formatter):
    """Форматтер для консоли: показывает только сообщение, без трейсбека.

    Полный трейсбек (кто именно и на какой строке упал) всё равно попадает
    в sync.log — он нужен для разбора. А в консоли планировщика хватает
    одной понятной строки.
    """

    def formatException(self, ei):  # noqa: N802 — имя метода задано stdlib
        return ""

    def format(self, record):
        saved_exc_info, saved_text = record.exc_info, record.exc_text
        record.exc_info, record.exc_text = None, None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved_exc_info, saved_text


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    """Настраиваем вывод и в файл, и в консоль.

    RotatingFileHandler сам обрежет лог, когда он дорастёт до 1 МБ,
    и оставит 3 предыдущих файла — чтобы диск не забился за месяцы работы.
    """
    # На Windows консоль по умолчанию не в UTF-8, и русские буквы могут
    # уронить вывод с UnicodeEncodeError. Переключаем поток вывода на UTF-8;
    # если консоль этого не умеет — просто работаем дальше, лог-файл всё равно
    # пишется в UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

    level = logging.DEBUG if verbose else getattr(logging, cfg.log_level, logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        cfg.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        ConsoleFormatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(console)

    # Библиотеки Google очень болтливы на уровне INFO — приглушаем их.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_timezone(cfg: Config) -> ZoneInfo:
    """Часовой пояс магазина. Если база часовых поясов недоступна — работаем в UTC."""
    try:
        return ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        log.warning(
            "Часовой пояс %r не найден (установите пакет tzdata), использую UTC",
            cfg.timezone,
        )
        return ZoneInfo("UTC")


# ----------------------------------------------------------------------
# БЛОК 2. Период выборки
# ----------------------------------------------------------------------

def calculate_period(
    cfg: Config,
    state: SyncState,
    tz: ZoneInfo,
    days_override: int | None,
    since: date | None = None,
):
    """Считаем, за какой период просить продажи.

    Логика:
      * если задан --since ГГГГ-ММ-ДД, берём продажи с начала этого дня;
      * если задан --days N, берём последние N дней;
      * если это первый запуск (нет last_sync_at), берём SYNC_DAYS_BACK дней;
      * иначе — от времени прошлой синхронизации минус «перекрытие»
        SYNC_OVERLAP_DAYS. Перекрытие нужно, потому что фильтр работает
        по датам, а не по секундам: лучше лишний раз перечитать день
        и отсеять дубликаты по ID, чем потерять продажу.
    """
    date_to = datetime.now(tz)

    if since is not None:
        # Полночь указанного дня по времени магазина: «с 1 июня» — это
        # с 01.06 00:00 по Алматы, а не с какого-то часа 31 мая.
        date_from = datetime.combine(since, time.min, tzinfo=tz)
        log.info("Период задан вручную: с %s", date_from.strftime("%d.%m.%Y"))
    elif days_override is not None:
        date_from = date_to - timedelta(days=days_override)
        log.info("Период задан вручную: последние %d дн.", days_override)
    elif state.last_sync_at is None:
        date_from = date_to - timedelta(days=cfg.days_back)
        log.info("Первый запуск: беру продажи за последние %d дн.", cfg.days_back)
    else:
        date_from = state.last_sync_at.astimezone(tz) - timedelta(days=cfg.overlap_days)
        log.info(
            "Продолжаю с прошлой синхронизации (%s) с перекрытием %d дн.",
            state.last_sync_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
            cfg.overlap_days,
        )

    return date_from, date_to


# ----------------------------------------------------------------------
# БЛОК 3. Получение данных: из реального API или фейковых
# ----------------------------------------------------------------------

@dataclass
class AccountBatch:
    """Всё, что собрано по одному аккаунту Wipon за этот запуск."""

    account: WiponAccount
    state: SyncState          # память этого аккаунта (state.json / state_2.json)
    started_at: datetime      # станет last_sync_at после успешной записи
    sales: list               # продажи, которых ещё нет в state
    item_titles: dict         # справочник названий товаров этого аккаунта
    written: int = 0          # сколько строк записано за запуск

    def rows(self, cfg: Config, tz: ZoneInfo) -> List[SaleRow]:
        """Строки таблицы; филиалы получают приставку аккаунта."""
        return sales_to_rows(
            self.sales, self.item_titles, tz, cfg.item_filter, self.account.label
        )


def collect_sales(cfg: Config, account: WiponAccount, tz: ZoneInfo, args, state: SyncState):
    """Возвращаем пару (список продаж, справочник названий товаров) одного аккаунта."""
    if args.test:
        from fake_data import fake_item_titles, fake_sales

        log.warning("РЕЖИМ ТЕСТА: реальный API не вызывается, данные сгенерированы локально")
        # У каждого аккаунта свой префикс тестовых ID: TEST, TEST2, TEST3...
        id_prefix = "TEST" if account.number == 1 else f"TEST{account.number}"
        return fake_sales(tz, count=args.fake_count, id_prefix=id_prefix), fake_item_titles()

    # Боевой режим: работаем с настоящим API.
    from wipon_api import WiponClient

    account.validate()
    client = WiponClient(cfg, account)

    date_from, date_to = calculate_period(cfg, state, tz, args.days, args.since)
    sales = client.fetch_sales(date_from, date_to)

    # Один раз при первом реальном запуске сохраняем сырой JSON продажи.
    # В документации примера ответа для списка продаж нет, поэтому файл
    # raw_sale_sample.json — способ сверить имена полей с тем, что мы разбираем.
    if sales and not account.raw_sample_file.is_file():
        account.raw_sample_file.write_text(
            json.dumps(sales[0], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(
            "Сохранил пример ответа API в %s — сверьте имена полей, если строки выглядят пустыми",
            account.raw_sample_file.name,
        )

    # Справочник товаров нужен, только если в самих продажах нет названий.
    item_titles = client.get_item_titles(force_refresh=args.refresh_items)
    return sales, item_titles


def collect_account(cfg: Config, account: WiponAccount, tz: ZoneInfo, args) -> AccountBatch:
    """Забираем продажи одного аккаунта и отсекаем уже записанные."""
    log.info("--- %s ---", account.title)
    state = SyncState(account.state_file, cfg.max_remembered_ids)
    if args.reset_state:
        state.reset()

    started_at = datetime.now(tz)
    sales, item_titles = collect_sales(cfg, account, tz, args, state)
    fresh = sorted(filter_new_sales(sales, state), key=sale_sort_key)
    log.info("%s: получено продаж %d, из них новых %d", account.title, len(sales), len(fresh))
    return AccountBatch(account, state, started_at, fresh, item_titles)


# ----------------------------------------------------------------------
# БЛОК 4. Фильтрация дублей
# ----------------------------------------------------------------------

def filter_new_sales(
    sales: Sequence[Mapping[str, Any]],
    state: SyncState,
    known_ids: Set[str] = frozenset(),
) -> List[Mapping[str, Any]]:
    """Оставляем только те продажи, которых ещё нет в таблице.

    state     — память аккаунта: какие продажи он уже записывал;
    known_ids — ID продаж, которые уже стоят на листе (сверка с таблицей).
    """
    fresh = []
    skipped = 0
    for sale in sales:
        sale_id = sale.get("id")
        if sale_id is None:
            continue
        if state.is_processed(str(sale_id)) or str(sale_id) in known_ids:
            skipped += 1
            continue
        fresh.append(sale)

    if skipped:
        log.info("Пропущено уже записанных продаж: %d", skipped)
    return fresh


def print_rows(account: WiponAccount, rows: Sequence[SaleRow]) -> None:
    """Показываем строки в консоли (режим --dry-run)."""
    print(f"\n--- {account.title}: строки, которые были бы записаны в таблицу ---")
    print(
        f"{'Дата и время':<21}{'Филиал':<30}{'Товар':<32}"
        f"{'Кол-во':>8}{'Цена':>10}{'Сумма':>11}  ID"
    )
    for row in rows:
        title = row.title if len(row.title) <= 30 else row.title[:29] + "…"
        branch = row.branch if len(row.branch) <= 28 else row.branch[:27] + "…"
        print(
            f"{row.sold_at:<21}{branch:<30}{title:<32}{row.quantity:>8.3f}"
            f"{row.unit_price:>10.2f}{row.total:>11.2f}  {row.sale_id}"
        )
    print(f"--- Итого строк: {len(rows)} ---\n")


# ----------------------------------------------------------------------
# БЛОК 5. Основной сценарий
# ----------------------------------------------------------------------

def sale_ids_from_blocks(by_branch: dict) -> set:
    """Собираем ID продаж из прочитанных блоков — для защиты от дублей."""
    ids = set()
    for rows in by_branch.values():
        for row in rows:
            sale_id = str(row[5]).strip() if len(row) > 5 else ""
            if sale_id:
                ids.add(sale_id)
    return ids


def update_summary_sheet(cfg: Config, tz, by_branch: dict, sort_key) -> None:
    """Обновляем лист «Сводка» по блокам, которые лежат в таблице.

    Ошибку наверх не пропускаем: сводка — витрина, из-за неё не должен
    падать запуск, продажи к этому моменту уже записаны.
    """
    if not cfg.summary_sheet_name:
        return                       # сводка отключена в настройках

    from sheets import SheetsWriter

    try:
        SheetsWriter(cfg).write_summary(
            cfg.item_filter,
            build_branch_summary(by_branch),
            datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S"),
            sort_key,
        )
    except Exception as exc:                      # noqa: BLE001
        log.warning("Не удалось обновить сводку: %s", exc)


def save_states(batches: Sequence[AccountBatch]) -> None:
    """Запоминаем время синхронизации каждого успешно отработавшего аккаунта."""
    for batch in batches:
        batch.state.last_sync_at = batch.started_at
        batch.state.save()


def exit_code(failures: Sequence[Exception]) -> int:
    """Код возврата процесса: 0 — всё хорошо, 2 — у какого-то аккаунта
    не заполнены настройки, 1 — другая ошибка (сеть, API)."""
    if not failures:
        return 0
    return 2 if any(isinstance(exc, ConfigError) for exc in failures) else 1


def run(args) -> int:
    """Один цикл синхронизации. Возвращает код возврата процесса."""
    cfg = Config()
    setup_logging(cfg, verbose=args.verbose)
    tz = get_timezone(cfg)

    # Флаг --account N: работаем только с одним аккаунтом (например, чтобы
    # дозалить историю одной кассы, не трогая другую). Остальные аккаунты
    # не опрашиваются, но их блоки на листе сохраняются как есть.
    accounts = cfg.accounts
    if args.account is not None:
        accounts = [a for a in cfg.accounts if a.number == args.account]
        if not accounts:
            raise ConfigError(
                f"Аккаунт {args.account} не найден в .env "
                f"(нужна переменная WIPON_{args.account}_USERNAME)"
            )

    log.info("=" * 70)
    log.info(
        "Старт синхронизации%s, аккаунтов Wipon: %d%s",
        " (тестовый режим)" if args.test else "",
        len(accounts),
        f" (только аккаунт {args.account})" if args.account is not None else "",
    )

    # --- Шаг 1: забираем продажи каждого аккаунта ---
    # Аккаунты независимы: если касса одного недоступна, продажи другого
    # всё равно попадут в таблицу. Упавший аккаунт не обновляет своё
    # состояние, поэтому следующий запуск по расписанию его повторит.
    batches: List[AccountBatch] = []
    failures: List[Exception] = []
    for account in accounts:
        try:
            batches.append(collect_account(cfg, account, tz, args))
        except Exception as exc:                  # noqa: BLE001
            failures.append(exc)
            # У ошибки настройки трейсбек бесполезен — хватает текста.
            log.error(
                "%s: продажи не получены: %s",
                account.title,
                exc,
                exc_info=not isinstance(exc, ConfigError),
            )

    # --- Шаг 2: режим просмотра — только показываем строки ---
    if args.dry_run:
        for batch in batches:
            print_rows(batch.account, batch.rows(cfg, tz))
        log.info("Режим --dry-run: в таблицу ничего не записано, состояние не изменено")
        return exit_code(failures)

    block_order = make_block_sort_key(cfg.account_labels())

    if not any(batch.sales for batch in batches):
        log.info("Новых продаж нет. Записано строк: 0")
        save_states(batches)
        if cfg.summary_sheet_name:
            from sheets import SheetsWriter

            writer = SheetsWriter(cfg)
            writer.ensure_sheet()
            update_summary_sheet(cfg, tz, writer.read_branch_blocks(), block_order)
        return exit_code(failures)

    # --- Шаг 3: читаем лист ---
    from sheets import SheetsWriter

    writer = SheetsWriter(cfg)
    writer.ensure_sheet()

    # Таблица — первоисточник: скрипт не хранит продажи у себя, а блоки
    # перезаписываются целиком, поэтому уже записанное нужно вычитать
    # и дополнить новыми строками. Блоки всех аккаунтов лежат на одном листе,
    # так что читаем их один раз и пишем тоже один раз.
    by_branch = writer.read_branch_blocks()

    # Вторая проверка на дубли — уже по самой таблице. Помогает, если
    # state.json потеряли или скрипт запустили на другом компьютере.
    sheet_ids: Set[str] = set()
    if cfg.reconcile_with_sheet:
        sheet_ids = sale_ids_from_blocks(by_branch)
        log.info("В таблице уже записано продаж: %d", len(sheet_ids))

    # --- Шаг 4: раскладываем новые строки по блокам филиалов ---
    written = 0
    for batch in batches:
        new_sales = filter_new_sales(batch.sales, batch.state, sheet_ids)
        batch_rows = sales_to_rows(
            new_sales, batch.item_titles, tz, cfg.item_filter, batch.account.label
        )
        for row in batch_rows:
            by_branch.setdefault(row.branch, []).append(row.as_block_row())
        batch.written = len(batch_rows)
        written += batch.written

    if written:
        # Сортируем каждый блок, чтобы продажи шли по порядку, а не хвостом.
        for branch_rows in by_branch.values():
            branch_rows.sort(key=sale_row_sort_key)
        writer.write_branch_blocks(by_branch, block_order)
    else:
        log.info("После сверки с таблицей новых строк не осталось")

    # --- Шаг 5: запоминаем результат ---
    # Состояние обновляем ТОЛЬКО после успешной записи: если бы запись
    # упала, следующий запуск должен повторить эти же продажи.
    # Помечаем все продажи, прошедшие шаг 1: они либо записаны сейчас,
    # либо уже стояли в таблице.
    for batch in batches:
        batch.state.mark_processed(str(s["id"]) for s in batch.sales)
    save_states(batches)

    # --- Шаг 6: пересчитываем сводку по филиалам ---
    update_summary_sheet(cfg, tz, by_branch, block_order)

    for batch in batches:
        log.info(
            "ИТОГ %s: новых продаж %d, записано строк %d",
            batch.account.title,
            len(batch.sales),
            batch.written,
        )
    log.info("ИТОГ: записано строк всего %d", written)
    return exit_code(failures)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Синхронизация продаж Wipon (Prosklad) с Google Таблицей",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="тестовый режим: продажи берутся из fake_data.py, API Wipon не вызывается",
    )
    parser.add_argument(
        "--fake-count",
        type=int,
        default=4,
        help="сколько фейковых чеков сгенерировать в режиме --test (по умолчанию 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ничего не писать в таблицу, только показать строки в консоли",
    )
    parser.add_argument(
        "--days",
        type=int,
        help="разово забрать продажи за последние N дней (игнорирует сохранённое состояние)",
    )
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        metavar="ГГГГ-ММ-ДД",
        help="разово забрать продажи начиная с этой даты, например --since 2026-06-01",
    )
    parser.add_argument(
        "--account",
        type=int,
        metavar="N",
        help="работать только с аккаунтом N из .env (1, 2...); блоки остальных не трогаются",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="забыть, какие продажи уже записаны (удаляет state.json)",
    )
    parser.add_argument(
        "--refresh-items",
        action="store_true",
        help="принудительно перезагрузить справочник товаров, не заглядывая в кеш",
    )
    parser.add_argument("--verbose", action="store_true", help="подробный лог (уровень DEBUG)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """Точка входа.

    Здесь ловим ВСЕ исключения: скрипт запускается планировщиком, и падение
    с трейсбеком никто не увидит. Вместо этого пишем ошибку в лог и выходим
    с кодом 1 — следующий запуск по расписанию попробует снова.
    """
    args = parse_args(argv)
    try:
        return run(args)
    except ConfigError as exc:
        # Ошибка настройки (нет переменной в .env, не найден JSON-ключ).
        # Отдельный код возврата 2 — чтобы в планировщике было видно,
        # что это не сбой сети, а незаконченная настройка.
        logging.getLogger("wipon_sync").error("Ошибка конфигурации: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Прервано пользователем")
        return 130
    except Exception as exc:  # noqa: BLE001 — намеренно ловим всё
        # exc_info=True — полный трейсбек уходит в sync.log,
        # в консоли останется только одна строка с текстом ошибки.
        log.error("Синхронизация не удалась: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
