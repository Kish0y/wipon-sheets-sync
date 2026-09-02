"""
wipon_api.py — работа с API кассовой системы Wipon (бывш. Prosklad).

Что здесь есть:
  1. Получение и кеширование access_token (POST /v1/oauth/token).
  2. Получение списка продаж (GET /v1/employee/{employee_id}/sale).
  3. Получение списка товаров (GET /v2/employee/{employee_id}/item) с кешем на диске.

Все имена полей и параметров взяты из официальной документации
"Документация API Prosklad".
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from config import Config

log = logging.getLogger(__name__)


class WiponError(Exception):
    """Любая ошибка при общении с Wipon: сеть, неверный ответ, отказ сервера."""


class WiponAuthError(WiponError):
    """Отдельно выделяем ошибку авторизации (401/403), чтобы уметь перелогиниться."""


class WiponClient:
    """Клиент API Wipon.

    Хранит внутри себя requests.Session (одно TCP-соединение переиспользуется)
    и текущий access_token.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        # Заголовки, общие для всех запросов. Accept: application/json —
        # так API отвечает JSON-ом, а не HTML-страницей ошибки.
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
        )
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # БЛОК 1. Авторизация и кеш токена
    # ------------------------------------------------------------------

    def _load_token_from_disk(self) -> bool:
        """Пробуем поднять токен из файла token.json.

        Возвращаем True, если токен есть и он ещё не протух.
        Токен по документации живёт год (expires_in = 31536000 секунд),
        поэтому дёргать /oauth/token при каждом запуске бессмысленно.
        """
        path = self.cfg.token_file
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Не удалось прочитать %s (%s), получим токен заново", path.name, exc)
            return False

        token = data.get("access_token")
        expires_at = float(data.get("expires_at") or 0)
        if not token:
            return False

        # Обновляем заранее, за 7 дней до истечения, чтобы не поймать
        # просроченный токен посреди рабочего дня.
        safety_margin = 7 * 24 * 3600
        if time.time() + safety_margin >= expires_at:
            log.info("Сохранённый токен скоро истекает — запросим новый")
            return False

        self._access_token = token
        self._token_expires_at = expires_at
        left_days = int((expires_at - time.time()) / 86400)
        log.info("Использую сохранённый токен из %s (осталось ~%d дн.)", path.name, left_days)
        return True

    def _save_token_to_disk(self, token: str, expires_in: int, company_id: Any) -> None:
        """Кладём токен на диск, чтобы следующий запуск его переиспользовал."""
        expires_at = time.time() + int(expires_in)
        payload = {
            "access_token": token,
            "expires_in": int(expires_in),
            "expires_at": expires_at,
            "company_id": company_id,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cfg.token_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._access_token = token
        self._token_expires_at = expires_at

    def authenticate(self, force: bool = False) -> str:
        """Возвращаем действующий access_token.

        force=True заставляет запросить новый токен, даже если старый лежит в кеше
        (нужно, когда сервер ответил 401 — значит токен отозвали).
        """
        if not force and (self._access_token or self._load_token_from_disk()):
            return self._access_token  # type: ignore[return-value]

        self.cfg.validate_wipon()
        url = f"{self.cfg.api_url}/v1/oauth/token"
        log.info("Запрашиваю новый access_token: POST %s", url)

        # Тело запроса ровно как в документации: username и password.
        response = self._request(
            "POST",
            url,
            json={"username": self.cfg.username, "password": self.cfg.password},
            with_auth=False,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise WiponAuthError(f"Ответ /oauth/token не является JSON: {exc}") from exc

        token = data.get("access_token")
        if not token:
            raise WiponAuthError(f"В ответе /oauth/token нет access_token: {data}")

        # expires_in в документации ≈ 31536000 (год). Если поля вдруг нет —
        # считаем, что токен живёт сутки, чтобы не улететь в вечный кеш.
        expires_in = int(data.get("expires_in") or 86400)
        self._save_token_to_disk(token, expires_in, data.get("company_id"))
        log.info("Токен получен, expires_in=%s сек, company_id=%s", expires_in, data.get("company_id"))
        return token

    # ------------------------------------------------------------------
    # БЛОК 2. Низкоуровневый HTTP с повторами
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        with_auth: bool = True,
    ) -> requests.Response:
        """Одна HTTP-операция с повторами при сетевых сбоях и 5xx.

        Сетевые ошибки и 500-е повторяем несколько раз с нарастающей паузой:
        касса могла на секунду отвалиться, это нормально.
        401/403 не повторяем — их обрабатывает вызывающий код (перелогин).
        """
        headers: Dict[str, str] = {}
        if with_auth:
            headers["Authorization"] = f"Bearer {self.authenticate()}"

        last_error: Optional[str] = None
        for attempt in range(1, self.cfg.http_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=self.cfg.http_timeout,
                )
            except requests.RequestException as exc:
                last_error = f"сетевая ошибка: {exc}"
                log.warning("Попытка %d/%d не удалась (%s)", attempt, self.cfg.http_retries, last_error)
            else:
                if response.status_code in (401, 403):
                    raise WiponAuthError(
                        f"Wipon вернул {response.status_code} на {url}: {response.text[:300]}"
                    )
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    log.warning("Попытка %d/%d: %s", attempt, self.cfg.http_retries, last_error)
                elif not response.ok:
                    # 4xx (кроме 401/403/429) повторять бессмысленно — это наша ошибка в запросе.
                    raise WiponError(f"HTTP {response.status_code} на {url}: {response.text[:500]}")
                else:
                    return response

            if attempt < self.cfg.http_retries:
                time.sleep(2 ** attempt)  # 2, 4, 8 секунд

        raise WiponError(f"Запрос {method} {url} не удался после {self.cfg.http_retries} попыток. {last_error}")

    def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET, который умеет один раз перелогиниться при 401/403."""
        try:
            response = self._request("GET", url, params=params)
        except WiponAuthError as exc:
            log.warning("Токен отклонён (%s). Получаю новый и повторяю запрос", exc)
            self.authenticate(force=True)
            response = self._request("GET", url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise WiponError(f"Ответ {url} не является JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # БЛОК 3. Продажи
    # ------------------------------------------------------------------

    def fetch_sales(self, date_from: datetime, date_to: datetime) -> List[Dict[str, Any]]:
        """Забираем продажи за период.

        Эндпоинт из документации:
            GET {url}/v1/employee/{employee_id}/sale
        Фильтры date_from / date_to задокументированы как тип "date",
        формат берём из настройки WIPON_DATE_FORMAT.

        Пагинация: документация описывает page/per_page для списка товаров,
        для продаж явного описания нет, поэтому идём по страницам и
        останавливаемся, как только страница вернулась неполной или пустой.
        """
        url = f"{self.cfg.api_url}/v1/employee/{self.cfg.employee_id}/sale"
        sales: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for page in range(1, self.cfg.max_pages + 1):
            params: Dict[str, Any] = {
                "date_from": date_from.strftime(self.cfg.date_format),
                "date_to": date_to.strftime(self.cfg.date_format),
                "page": page,
                "per_page": self.cfg.per_page,
            }
            # stock_id — необязательный фильтр по складу из документации.
            if self.cfg.stock_id:
                params["stock_id"] = self.cfg.stock_id

            log.debug("GET %s params=%s", url, params)
            payload = self._get_json(url, params)
            batch = _extract_list(payload)
            if not batch:
                break

            # Защита от API, которое игнорирует page и всё время отдаёт первую
            # страницу: если новых ID нет — прекращаем, иначе будет вечный цикл.
            new_in_batch = 0
            for sale in batch:
                sale_id = sale.get("id")
                key = str(sale_id)
                if sale_id is not None and key in seen_ids:
                    continue
                seen_ids.add(key)
                sales.append(sale)
                new_in_batch += 1

            log.info("Страница %d: получено %d продаж (новых %d)", page, len(batch), new_in_batch)
            if new_in_batch == 0 or len(batch) < self.cfg.per_page:
                break

        log.info(
            "Всего получено продаж за период %s — %s: %d",
            date_from.strftime(self.cfg.date_format),
            date_to.strftime(self.cfg.date_format),
            len(sales),
        )
        return sales

    # ------------------------------------------------------------------
    # БЛОК 4. Товары (справочник названий) + кеш на диске
    # ------------------------------------------------------------------

    def fetch_items(self) -> List[Dict[str, Any]]:
        """Скачиваем весь справочник товаров: GET {url}/v2/employee/{employee_id}/item.

        Поля из документации, которые нам нужны: id, title, barcode.
        Пагинация: параметры page и per_page задокументированы.
        """
        url = f"{self.cfg.api_url}/v2/employee/{self.cfg.employee_id}/item"
        items: List[Dict[str, Any]] = []

        for page in range(1, self.cfg.max_pages + 1):
            params = {"page": page, "per_page": self.cfg.per_page}
            payload = self._get_json(url, params)
            batch = _extract_list(payload)
            if not batch:
                break
            items.extend(batch)
            log.debug("Товары, страница %d: %d шт.", page, len(batch))
            if len(batch) < self.cfg.per_page:
                break

        log.info("Справочник товаров загружен: %d позиций", len(items))
        return items

    def get_item_titles(self, force_refresh: bool = False) -> Dict[str, str]:
        """Возвращаем словари для поиска названия товара.

        Результат — плоский словарь, где ключи это:
          "id:<item_id>"    -> название
          "barcode:<штрихкод>" -> название
        Такой кеш лежит в items_cache.json и обновляется раз в ITEMS_CACHE_TTL_HOURS,
        чтобы не дёргать API на каждую строку продажи.
        """
        cache_path = self.cfg.items_cache_file
        ttl = timedelta(hours=self.cfg.items_cache_ttl_hours)

        if not force_refresh and cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                saved_at = datetime.fromisoformat(cached["saved_at"])
                if datetime.now(timezone.utc) - saved_at < ttl:
                    log.info("Использую кеш товаров (%d записей)", len(cached.get("titles", {})))
                    return cached.get("titles", {})
            except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
                log.warning("Кеш товаров повреждён (%s), перезагружаю справочник", exc)

        titles: Dict[str, str] = {}
        for item in self.fetch_items():
            title = item.get("title")
            if not title:
                continue
            if item.get("id") is not None:
                titles[f"id:{item['id']}"] = title
            if item.get("barcode"):
                titles[f"barcode:{item['barcode']}"] = title

        cache_path.write_text(
            json.dumps(
                {"saved_at": datetime.now(timezone.utc).isoformat(), "titles": titles},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return titles


def _extract_list(payload: Any) -> List[Dict[str, Any]]:
    """Достаём массив записей из ответа API.

    В документации ответы приходят в виде {"data": [ ... ]},
    но на всякий случай понимаем и «голый» список.
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
    return []
