"""
whoami.py — вспомогательный скрипт: показывает, кто вы для API Wipon.

Зачем он нужен. Для запроса продаж нужен employee_id — ID *сотрудника*.
Его легко перепутать с ID *пользователя* (того, кто вводит логин и пароль):
это разные числа. Если подставить не тот, Wipon отвечает
«403 У сотрудника нет нужного права или доступа к складу этой компании».

Скрипт логинится, дёргает GET /v1/user и печатает список сотрудников
из поля employees — оттуда и берётся значение для WIPON_EMPLOYEE_ID в .env.

Запуск:
    python whoami.py              # первый аккаунт (WIPON_USERNAME, WIPON_PASSWORD)
    python whoami.py --account 2  # второй аккаунт (WIPON_2_USERNAME, WIPON_2_PASSWORD)
"""

import argparse
import sys

import requests

from config import Config, ConfigError


def main() -> int:
    parser = argparse.ArgumentParser(description="Показать employee_id аккаунта Wipon")
    parser.add_argument(
        "--account", type=int, default=1, help="номер аккаунта из .env (по умолчанию 1)"
    )
    args = parser.parse_args()

    try:
        cfg = Config()
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}")
        return 2

    account = next((a for a in cfg.accounts if a.number == args.account), None)
    if account is None:
        print(
            f"Аккаунт {args.account} не найден в .env. "
            f"Для него нужна хотя бы переменная WIPON_{args.account}_USERNAME."
        )
        return 2

    # Для whoami employee_id ещё может быть неизвестен — его мы и ищем,
    # поэтому проверяем только логин и пароль.
    missing = [
        account.prefix + name
        for name, value in (("USERNAME", account.username), ("PASSWORD", account.password))
        if not value
    ]
    if missing:
        print(f"Ошибка настройки: в .env не заданы {', '.join(missing)}")
        return 2

    # --- Шаг 1: получаем токен ---
    # Отдельно от wipon_api.py: тот кеширует токен в token.json, а здесь
    # нам нужен простой одноразовый запрос, без побочных эффектов.
    print(f"{account.title}: авторизуюсь в Wipon...")
    try:
        response = requests.post(
            f"{cfg.api_url}/v1/oauth/token",
            json={"username": account.username, "password": account.password},
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
        # У владельца компании списка прав в ответе может не быть вовсе —
        # вместо него стоит is_owner, и доступ у него полный. Поэтому пустой
        # список прав ещё не значит «нельзя»: точно покажет пробный запуск.
        role = employee.get("role") or {}
        permissions = role.get("permissions") or []
        can_see_sales = any(
            p.get("group") == "sale" and p.get("ability") == "show" for p in permissions
        )
        if role.get("is_owner"):
            mark = "✓ владелец, полный доступ"
        elif can_see_sales:
            mark = "✓ может смотреть продажи"
        elif not permissions:
            mark = "? права не указаны — проверьте: python wipon_sync.py --dry-run"
        else:
            mark = "✗ нет права на продажи"
        print(
            f"  {account.prefix}EMPLOYEE_ID={employee.get('id')}"
            f"  | {employee.get('name')}"
            f" | роль: {employee.get('role_name')}"
            f" | компания: {employee.get('company_name')}"
            f" | {mark}"
        )

    print(f"\nСкопируйте подходящую строку {account.prefix}EMPLOYEE_ID=... в файл .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
