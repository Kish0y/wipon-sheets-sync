#!/bin/bash
# setup.sh — установка синхронизации Wipon на сервер Ubuntu.
#
# Запускать НА СЕРВЕРЕ, после того как файлы проекта скопированы
# в /opt/wipon-sync:
#     sudo bash /opt/wipon-sync/deploy/setup.sh
#
# Скрипт можно запускать повторно — он ничего не ломает,
# просто заново приводит систему в нужное состояние.

set -e  # при первой же ошибке останавливаемся, чтобы не чинить полсистемы

APP_DIR="/opt/wipon-sync"
APP_USER="ubuntu"        # стандартный пользователь в образах Ubuntu на Oracle

echo "==> 1. Ставим Python и утилиты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

echo "==> 2. Часовой пояс сервера — Asia/Almaty"
# Сервер Oracle по умолчанию живёт в UTC. Сам скрипт часовой пояс
# учитывает сам (TIMEZONE в .env), но с местным временем читать
# системные логи гораздо удобнее.
timedatectl set-timezone Asia/Almaty

echo "==> 3. Проверяем файлы проекта"
cd "$APP_DIR"
for required in wipon_sync.py requirements.txt .env service_account.json; do
    if [ ! -f "$required" ]; then
        echo "ОШИБКА: не найден $APP_DIR/$required"
        echo "Скопируйте его с рабочего компьютера командой scp и запустите setup.sh снова."
        exit 1
    fi
done

echo "==> 4. Виртуальное окружение и зависимости"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> 5. Права: файлы с паролями читает только владелец"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env" "$APP_DIR/service_account.json"

echo "==> 6. Пробный запуск от имени $APP_USER"
# Если тут ошибка — дальше настраивать таймер бессмысленно.
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/wipon_sync.py" --days 1 --dry-run

echo "==> 7. Ставим systemd-юниты и включаем таймер"
cp "$APP_DIR/deploy/wipon-sync.service" /etc/systemd/system/
cp "$APP_DIR/deploy/wipon-sync.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wipon-sync.timer

echo
echo "==> ГОТОВО. Таймер запущен."
echo
systemctl list-timers wipon-sync.timer --no-pager
echo
echo "Полезные команды:"
echo "  systemctl list-timers wipon-sync.timer   # когда следующий запуск"
echo "  journalctl -u wipon-sync.service -n 50   # что было в последних запусках"
echo "  systemctl start wipon-sync.service       # запустить прямо сейчас"
echo "  tail -f $APP_DIR/sync.log                # лог самого скрипта"
