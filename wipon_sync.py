#!/usr/bin/env python
"""
wipon_sync.py — главный файл. Переносит продажи из кассы Wipon в Google Таблицу.

Как это работает за один запуск:
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
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any, List, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import Config, ConfigError
from state import SyncState
from transform import build_branch_summary, SaleRow, sale_sort_key, sales_to_rows

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

def calculate_period(cfg: Config, state: SyncState, tz: ZoneInfo, days_override: int | None):
    """Считаем, за какой период просить продажи.

    Логика:
      * если задан --days N, берём последние N дней;
      * если это первый запуск (нет last_sync_at), берём SYNC_DAYS_BACK дней;
      * иначе — от времени прошлой синхронизации минус «перекрытие»
        SYNC_OVERLAP_DAYS. Перекрытие нужно, потому что фильтр работает
        по датам, а не по секундам: лучше лишний раз перечитать день
        и отсеять дубликаты по ID, чем потерять продажу.
    """
    date_to = datetime.now(tz)

    if days_override is not None:
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

def collect_sales(cfg: Config, tz: ZoneInfo, args, state: SyncState):
    """Возвращаем пару (список продаж, справочник названий товаров)."""
    if args.test:
        from fake_data import fake_item_titles, fake_sales

        log.warning("РЕЖИМ ТЕСТА: реальный API не вызывается, данные сгенерированы локально")
        return fake_sales(tz, count=args.fake_count), fake_item_titles()

    # Боевой режим: работаем с настоящим API.
    from wipon_api import WiponClient

    cfg.validate_wipon()
    client = WiponClient(cfg)

    date_from, date_to = calculate_period(cfg, state, tz, args.days)
    sales = client.fetch_sales(date_from, date_to)

    # Один раз при первом реальном запуске сохраняем сырой JSON продажи.
    # В документации примера ответа для списка продаж нет, поэтому файл
    # raw_sale_sample.json — способ сверить имена полей с тем, что мы разбираем.
    if sales and not cfg.raw_sample_file.is_file():
        cfg.raw_sample_file.write_text(
            json.dumps(sales[0], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(
            "Сохранил пример ответа API в %s — сверьте имена полей, если строки выглядят пустыми",
            cfg.raw_sample_file.name,
        )

    # Справочник товаров нужен, только если в самих продажах нет названий.
    item_titles = client.get_item_titles(force_refresh=args.refresh_items)
    return sales, item_titles


# ----------------------------------------------------------------------
# БЛОК 4. Фильтрация дублей
# ----------------------------------------------------------------------

def filter_new_sales(sales: Sequence[Mapping[str, Any]], state: SyncState) -> List[Mapping[str, Any]]:
    """Оставляем только те продажи, которых ещё нет в таблице."""
    fresh = []
    skipped = 0
    for sale in sales:
        sale_id = sale.get("id")
        if sale_id is None:
            continue
        if state.is_processed(str(sale_id)):
            skipped += 1
            continue
        fresh.append(sale)

    if skipped:
        log.info("Пропущено уже записанных продаж: %d", skipped)
    return fresh


def print_rows(rows: Sequence[SaleRow]) -> None:
    """Показываем строки в консоли (режим --dry-run)."""
    print("\n--- Строки, которые были бы записаны в таблицу ---")
    print(
        f"{'Дата и время':<21}{'Филиал':<17}{'Товар':<32}"
        f"{'Кол-во':>8}{'Цена':>10}{'Сумма':>11}  ID"
    )
    for row in rows:
        title = row.title if len(row.title) <= 30 else row.title[:29] + "…"
        branch = row.branch if len(row.branch) <= 15 else row.branch[:14] + "…"
        print(
            f"{row.sold_at:<21}{branch:<17}{title:<32}{row.quantity:>8.3f}"
            f"{row.unit_price:>10.2f}{row.total:>11.2f}  {row.sale_id}"
        )
    print(f"--- Итого строк: {len(rows)} ---\n")


# ----------------------------------------------------------------------
# БЛОК 5. Основной сценарий
# ----------------------------------------------------------------------

def update_summary(cfg: Config, tz) -> None:
    """Пересчитываем лист «Сводка» по тому, что уже лежит в таблице.

    Считаем именно по таблице, а не по свежей порции продаж: иначе после
    запуска, в котором не было новых чеков, сводка обнулилась бы.
    Ошибку сюда не пропускаем наверх — сводка это витрина, из-за неё
    не должен падать весь запуск, продажи уже записаны.
    """
    from sheets import SheetsWriter

    try:
        writer = SheetsWriter(cfg)
        summary = build_branch_summary(writer.read_data_rows())
        writer.write_summary(
            cfg.item_filter,
            summary,
            datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S"),
        )
    except Exception as exc:                      # noqa: BLE001
        log.warning("Не удалось обновить сводку по филиалам: %s", exc)


def run(args) -> int:
    """Один цикл синхронизации. Возвращает код возврата процесса."""
    cfg = Config()
    setup_logging(cfg, verbose=args.verbose)
    tz = get_timezone(cfg)

    log.info("=" * 70)
    log.info("Старт синхронизации%s", " (тестовый режим)" if args.test else "")

    state = SyncState(cfg.state_file, cfg.max_remembered_ids)
    if args.reset_state:
        state.reset()

    started_at = datetime.now(tz)

    # --- Шаг 1: забираем продажи ---
    sales, item_titles = collect_sales(cfg, tz, args, state)
    if not sales:
        log.info("Новых продаж нет. Обработано строк: 0")
        state.last_sync_at = started_at
        state.save()
        return 0

    # --- Шаг 2: отсекаем то, что уже записывали ---
    sales = sorted(filter_new_sales(sales, state), key=sale_sort_key)
    if not sales:
        log.info("Все полученные продажи уже были записаны раньше. Новых строк: 0")
        state.last_sync_at = started_at
        state.save()
        if not args.dry_run:
            update_summary(cfg, tz)
        return 0

    # --- Шаг 3: превращаем продажи в строки таблицы ---
    rows = sales_to_rows(sales, item_titles, tz, cfg.item_filter)
    log.info("Готово к записи: %d продаж -> %d строк", len(sales), len(rows))

    if args.dry_run:
        print_rows(rows)
        log.info("Режим --dry-run: в таблицу ничего не записано, состояние не изменено")
        return 0

    # --- Шаг 4: пишем в Google Таблицу ---
    from sheets import SheetsWriter

    writer = SheetsWriter(cfg)
    writer.ensure_sheet_and_header()

    # Вторая проверка на дубли — уже по самой таблице. Помогает, если
    # state.json потеряли или скрипт запустили на другом компьютере.
    if cfg.reconcile_with_sheet:
        state.add_known_ids(writer.existing_sale_ids())
        sales = sorted(filter_new_sales(sales, state), key=sale_sort_key)
        rows = sales_to_rows(sales, item_titles, tz, cfg.item_filter)
        if not rows:
            log.info("После сверки с таблицей новых строк не осталось")
            state.last_sync_at = started_at
            state.save()
            update_summary(cfg, tz)
            return 0

    written = writer.append_rows([r.as_sheet_row() for r in rows])

    # --- Шаг 5: запоминаем результат ---
    # Состояние обновляем ТОЛЬКО после успешной записи: если бы запись
    # упала, следующий запуск должен повторить эти же продажи.
    state.mark_processed(str(s["id"]) for s in sales)
    state.last_sync_at = started_at
    state.save()

    # --- Шаг 6: пересчитываем сводку по филиалам ---
    update_summary(cfg, tz)

    log.info(
        "ИТОГ: обработано продаж %d, записано строк %d, последний ID продажи %s",
        len(sales),
        written,
        state.last_sale_id,
    )
    return 0


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
