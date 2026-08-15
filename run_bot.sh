#!/usr/bin/env bash
# Запуск TGTG-бота: активирует venv и стартует bot.py.
# Использование:
#   ./run_bot.sh            — в текущем терминале (Ctrl+C остановить)
#   ./run_bot.sh bg         — в фоне, лог в bot.log
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Нет .venv — создай его: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

source .venv/bin/activate

if [ "${1:-}" = "bg" ]; then
    nohup .venv/bin/python bot.py > bot.log 2>&1 &
    echo "Бот запущен в фоне (PID $!), лог: bot.log"
    echo "Остановить: pkill -f 'python bot.py'"
else
    exec .venv/bin/python bot.py
fi
