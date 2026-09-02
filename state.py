"""
state.py — память скрипта между запусками.

Скрипт запускается по расписанию (например, раз в 5 минут) и каждый раз
должен понимать: что уже записано, а что новое. Для этого рядом со скриптом
лежит файл state.json примерно такого вида:

    {
      "last_sync_at": "2026-09-02T19:05:11+05:00",
      "last_sale_id": 1451,
      "processed_sale_ids": ["1449", "1450", "1451"]
    }

  * last_sync_at — время удачной синхронизации, от него считаем date_from;
  * processed_sale_ids — ID уже записанных продаж, главный фильтр от дублей;
  * last_sale_id — просто для наглядности в логах и при отладке.

Файл переписывается только после успешной записи в таблицу. Если запись
не удалась, состояние не меняется, и следующий запуск повторит попытку.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Set

log = logging.getLogger(__name__)


class SyncState:
    def __init__(self, path: Path, max_remembered_ids: int = 10000):
        self.path = path
        self.max_remembered_ids = max_remembered_ids
        self.last_sync_at: Optional[datetime] = None
        self.last_sale_id: Optional[str] = None
        # Список храним, чтобы знать порядок (какие ID самые свежие),
        # множество — чтобы быстро проверять «есть/нет».
        self._ordered_ids: List[str] = []
        self._id_set: Set[str] = set()
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Читаем состояние с диска. Битый или отсутствующий файл — не ошибка."""
        if not self.path.is_file():
            log.info("Файл состояния %s не найден — это первый запуск", self.path.name)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Файл состояния повреждён (%s) — начинаю с чистого листа", exc)
            return

        raw_date = data.get("last_sync_at")
        if raw_date:
            try:
                self.last_sync_at = datetime.fromisoformat(raw_date)
            except ValueError:
                log.warning("Не разобрал last_sync_at=%r, игнорирую", raw_date)

        self.last_sale_id = data.get("last_sale_id")
        self._ordered_ids = [str(x) for x in data.get("processed_sale_ids", [])]
        self._id_set = set(self._ordered_ids)
        log.info(
            "Состояние загружено: последняя синхронизация %s, помню %d ID продаж",
            self.last_sync_at,
            len(self._id_set),
        )

    def save(self) -> None:
        """Сохраняем состояние. Пишем через временный файл, чтобы при сбое
        (например, отключили питание) не остаться с обрезанным state.json."""
        payload = {
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_sale_id": self.last_sale_id,
            "processed_sale_ids": self._ordered_ids[-self.max_remembered_ids :],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        log.debug("Состояние сохранено в %s", self.path.name)

    # ------------------------------------------------------------------

    def is_processed(self, sale_id: str) -> bool:
        """Записывали ли мы уже эту продажу?"""
        return str(sale_id) in self._id_set

    def mark_processed(self, sale_ids: Iterable[str]) -> None:
        """Запоминаем свежие ID продаж."""
        for sale_id in sale_ids:
            key = str(sale_id)
            if key not in self._id_set:
                self._id_set.add(key)
                self._ordered_ids.append(key)
                self.last_sale_id = key
        # Обрезаем хвост, чтобы файл не разрастался.
        if len(self._ordered_ids) > self.max_remembered_ids:
            dropped = self._ordered_ids[: -self.max_remembered_ids]
            self._ordered_ids = self._ordered_ids[-self.max_remembered_ids :]
            self._id_set.difference_update(dropped)

    def add_known_ids(self, sale_ids: Iterable[str]) -> None:
        """Добавляем ID, вычитанные прямо из таблицы (страховка от дублей)."""
        for sale_id in sale_ids:
            key = str(sale_id).strip()
            if key and key not in self._id_set:
                self._id_set.add(key)
                self._ordered_ids.append(key)

    def reset(self) -> None:
        """Полный сброс памяти (флаг --reset-state)."""
        self.last_sync_at = None
        self.last_sale_id = None
        self._ordered_ids = []
        self._id_set = set()
        if self.path.is_file():
            self.path.unlink()
        log.warning("Состояние сброшено: следующий запуск начнёт с нуля")
