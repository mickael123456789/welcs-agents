#!/usr/bin/env bash
# ОДНА КОМАНДА для всего: вход в GitHub + выгрузка проекта + запуск бота в облаке.
# После неё бот и ежедневные сводки работают на серверах GitHub 24/7,
# и компьютер можно выключать.
#
#   bash ~/welcs-agents/scripts/setup.sh
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
ROOT="$HOME/welcs-agents"
cd "$ROOT"

echo "============================================================"
echo "  Welcs agents → GitHub (облако, работает без компьютера)"
echo "============================================================"

# 1. GitHub CLI должен быть установлен
if ! command -v gh >/dev/null; then
  echo "❌ Не найден gh (GitHub CLI)."
  echo "   Установи: brew install gh   (или скачай с https://cli.github.com)"
  exit 1
fi

# 2. Вход в GitHub (если ещё не вошёл)
if ! gh auth status >/dev/null 2>&1; then
  echo
  echo "▶️  Шаг входа в GitHub. Отвечай стрелками + Enter:"
  echo "    • GitHub.com"
  echo "    • HTTPS"
  echo "    • Authenticate Git with your GitHub credentials → Yes"
  echo "    • Login with a web browser → скопируй код, нажми Enter, вставь в браузере"
  echo
  gh auth login
fi
echo "✓ Вошёл как: $(gh api user -q .login)"

# 3. Полный деплой (создать репо, залить код и секреты, запустить бота, погасить локальное)
bash scripts/deploy.sh
