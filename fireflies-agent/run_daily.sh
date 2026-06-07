#!/bin/zsh
# Обёртка для launchd/cron. Выбирает режим по времени суток:
#   до 14:00 → morning (напоминания о дедлайнах на сегодня)
#   после    → evening (итоги дня)
# Лог пишется в reports/agent.log.

HERE="${0:A:h}"
cd "$HERE" || exit 1

PYTHON="$HERE/../.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

HOUR=$(date +%H)
if [ "$HOUR" -lt 14 ]; then MODE=morning; else MODE=evening; fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S')  mode=$MODE =====" >> "$HERE/reports/agent.log"
"$PYTHON" "$HERE/run.py" --mode "$MODE" >> "$HERE/reports/agent.log" 2>&1
echo "exit=$? ----------------------------------------" >> "$HERE/reports/agent.log"
