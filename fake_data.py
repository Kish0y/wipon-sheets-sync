"""
fake_data.py — фейковые данные для режима --test.

Зачем это нужно: пока нет боевого доступа к API Wipon, можно проверить
всю вторую половину скрипта (разбор продаж, защиту от дублей, запись
в Google Таблицу). Структура фейковой продажи повторяет ту, которую
описывает документация для создания продажи (v2):

    items[0][item_id], items[0][quantity], items[0][selling_price]

плюс поля id и created_at, которые в документации встречаются
в примерах ответов по товарам.

ID тестовых продаж содержат дату и время запуска с точностью до минуты:
    TEST-20260902-1930-1
Благодаря этому:
  * два запуска в одну минуту не создадут дублей (видно, как работает защита);
  * запуск в следующую минуту допишет новую порцию тестовых строк.
"""

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

# Мини-справочник «товаров». Ключ — item_id, как в реальном API.
FAKE_ITEMS: List[Dict[str, Any]] = [
    {"id": 1, "title": "Вода питьевая 1.5 л", "barcode": "4870004301234", "selling_price": "350.00"},
    {"id": 2, "title": "Хлеб белый нарезной", "barcode": "4870004302345", "selling_price": "220.00"},
    {"id": 3, "title": "Молоко 2.5% 1 л", "barcode": "4870004303456", "selling_price": "590.00"},
    {"id": 4, "title": "Шоколад молочный 90 г", "barcode": "4870004304567", "selling_price": "780.00"},
    {"id": 5, "title": "Пакет-майка", "barcode": "4870004305678", "selling_price": "30.00"},
    # Реальное название из ассортимента магазина — чтобы на фейковых данных
    # можно было проверить фильтр ITEM_FILTER.
    {"id": 6, "title": "Магний 3x Nurelum", "barcode": "8680512628408", "selling_price": "12000.00"},
]

# Филиалы (в терминах API — склады). Настоящих в кассе три.
FAKE_BRANCHES = [
    {"id": 67854, "name": "Основной склад"},
    {"id": 69602, "name": "2 точка"},
    {"id": 73184, "name": "3 точка"},
]


def fake_item_titles() -> Dict[str, str]:
    """Тот же формат справочника, что возвращает WiponClient.get_item_titles()."""
    titles: Dict[str, str] = {}
    for item in FAKE_ITEMS:
        titles[f"id:{item['id']}"] = item["title"]
        titles[f"barcode:{item['barcode']}"] = item["title"]
    return titles


def fake_sales(tz: ZoneInfo, count: int = 4, seed: int | None = None) -> List[Dict[str, Any]]:
    """Генерируем несколько правдоподобных чеков.

    Каждый чек — словарь с id, created_at и списком items,
    то есть ровно то, что скрипт ожидает получить от реального API.
    """
    rnd = random.Random(seed)
    now = datetime.now(tz)
    # Метка времени в ID: одинаковая в пределах одной минуты.
    batch = now.strftime("%Y%m%d-%H%M")

    sales: List[Dict[str, Any]] = []
    for n in range(1, count + 1):
        # Раскидываем чеки по времени: самый старый — раньше всех.
        sold_at = now - timedelta(minutes=(count - n) * 7)

        # Структура повторяет боевой ответ Wipon: позиции лежат в item_sale,
        # название товара — во вложенном объекте item.
        positions = []
        for item in rnd.sample(FAKE_ITEMS, rnd.randint(1, 3)):
            quantity = rnd.choice([1, 1, 2, 3, 5])
            positions.append(
                {
                    "id": rnd.randint(700000000, 799999999),
                    "quantity": f"{quantity}.000",
                    "selling_price": item["selling_price"],
                    "price": item["selling_price"],
                    "item": {
                        "id": item["id"],
                        "title": item["title"],
                        "barcode": item["barcode"],
                        "selling_price": item["selling_price"],
                    },
                }
            )

        branch = rnd.choice(FAKE_BRANCHES)
        sales.append(
            {
                "id": f"TEST-{batch}-{n}",
                "created_at": sold_at.strftime("%Y-%m-%d %H:%M:%S"),
                "stock": branch,
                "item_sale": positions,
            }
        )

    return sales
