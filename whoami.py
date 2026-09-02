"""
whoami.py — вспомогательный скрипт: показывает, кто вы для API Wipon.

Зачем он нужен. Для запроса продаж нужен employee_id — ID *сотрудника*.
Его легко перепутать с ID *пользователя* (того, кто вводит логин и пароль):
это разные числа. Если подставить не тот, Wipon отвечает
«403 У сотрудника нет нужного права или доступа к складу этой компании».

Скрипт логинится, дёргает GET /v1/user и печатает список сотрудников
из поля employees — оттуда и берётся значение для WIPON_EMPLOYEE_ID в .env.

Запуск:
    python whoami.py
"""

import sys

import requests

from config import Config


def main() -> int:
    cfg = Config()
    # validate_wipon() проверит, что логин и пароль вообще заданы в .env
    try:
        cfg.validate_wipon()
    except Exception as exc:
        print(f"Ошибка настройки: {exc}")
        return 2

    # --- Шаг 1: получаем токен ---
    # Отдельно от wipon_api.py: тот кеширует токен в token.json, а здесь
    # нам нужен простой одноразовый запрос, без побочных эффектов.
    print("Авторизуюсь в Wipon...")
    try:
        response = requests.post(
            f"{cfg.api_url}/v1/oauth/token",
            json={"username": cfg.username, "password": cfg.password},
            timeout=cfg.http_timeout,
        )
    except requests.RequestException as exc:
        print(f"Сеть недоступна: {exc}")
        return 1

    data = response.json() if response.content else {}
    token = data.get("access_token")
    if not token:
        # Самая частая причина — логин записан через 8, а нужно через 7.
        print(f"HTTP {response.status_code}: {data.get('message') or response.text[:200]}")
        print("Подсказка: логин — это номер вида 77XXXXXXXXX, начиная с семёрки.")
        return 1
    print(f"Токен получен (компания {data.get('company_id')})\n")

    # --- Шаг 2: спрашиваем данные о пользователе ---
    user = requests.get(
        f"{cfg.api_url}/v1/user",
        headers={"Authorization": f"Bearer {token}"},
        timeout=cfg.http_timeout,
    ).json().get("data", {})

    print(f"Пользователь: {user.get('name')} (id пользователя = {user.get('id')})")
    print("ВНИМАНИЕ: этот id в .env НЕ подходит — нужен id сотрудника из списка ниже.\n")

    # --- Шаг 3: печатаем сотрудников ---
    employees = user.get("employees") or []
    if not employees:
        print("У пользователя нет сотрудников — обратитесь в поддержку Wipon.")
        return 1

    print("Доступные сотрудники:")
    for employee in employees:
        # Проверяем, есть ли у роли право смотреть продажи (group=sale, ability=show):
        # без него запрос списка продаж вернёт 403.
        role = employee.get("role") or {}
        permissions = role.get("permissions") or []
        can_see_sales = any(
            p.get("group") == "sale" and p.get("ability") == "show" for p in permissions
        )
        mark = "✓ может смотреть продажи" if can_see_sales else "✗ нет права на продажи"
        print(
            f"  WIPON_EMPLOYEE_ID={employee.get('id')}"
            f"  | {employee.get('name')}"
            f" | роль: {employee.get('role_name')}"
            f" | компания: {employee.get('company_name')}"
            f" | {mark}"
        )

    print("\nСкопируйте подходящую строку WIPON_EMPLOYEE_ID=... в файл .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
