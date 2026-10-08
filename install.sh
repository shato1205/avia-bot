#!/usr/bin/env bash
# Установка бота на сервер Ubuntu/Debian: бот работает постоянно и отвечает сразу.
# Запуск: sudo bash install.sh (из папки репозитория)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/avia-bot.env

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти через sudo: sudo bash $0"; exit 1
fi

echo "== Ставлю Python и git"
apt-get update -qq && apt-get install -y -qq python3 git >/dev/null

if [ ! -f "$ENV_FILE" ]; then
  echo "== Введи токены (они сохранятся только на этом сервере)"
  read -rp "Токен Telegram-бота (от @BotFather): " TG_TOKEN </dev/tty
  read -rp "Твой chat id (от @userinfobot): " TG_CHAT </dev/tty
  read -rp "Токен Travelpayouts: " TP_TOKEN </dev/tty
  cat > "$ENV_FILE" <<ENV
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_CHAT_ID=$TG_CHAT
TRAVELPAYOUTS_TOKEN=$TP_TOKEN
ENV
  chmod 600 "$ENV_FILE"
fi

echo "== Создаю службу avia-bot"
cat > /etc/systemd/system/avia-bot.service <<UNIT
[Unit]
Description=Бот дешёвых авиабилетов
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 -u $DIR/check_prices.py --serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now avia-bot
sleep 3
systemctl --no-pager --lines=5 status avia-bot || true
echo
echo "== Готово! Напиши боту /start в Telegram."
echo "Логи: journalctl -u avia-bot -f    Обновить: cd $DIR && git pull && systemctl restart avia-bot"
