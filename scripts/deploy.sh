#!/usr/bin/env bash
# Полный деплой в один заход (после `gh auth login`):
#   1) создаёт ПУБЛИЧНЫЙ репозиторий welcs-agents и пушит код
#   2) заливает секреты из локальных .env
#   3) запускает сводку и живого бота в облаке
#
#   bash scripts/deploy.sh
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
command -v gh >/dev/null || { echo "❌ Нет gh."; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "❌ Сначала войди:  ~/.local/bin/gh auth login"; exit 1; }

cd "$(cd "$(dirname "$0")/.." && pwd)"
REPO_NAME="welcs-agents"
OWNER="$(gh api user -q .login)"
FULL="$OWNER/$REPO_NAME"

# 0. Преполётная проверка: весь Python должен компилироваться, и фиксируем
#    актуальное состояние диска (в т.ч. свежие правки) в коммит перед пушем.
PY="$HOME/welcs-agents/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
echo "→ проверка компиляции…"
"$PY" -m py_compile fireflies-agent/*.py bitrix-agent/*.py || { echo "❌ Python не компилируется — деплой отменён."; exit 1; }
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -q -m "Деплой: актуальное состояние перед пушем в облако"
  echo "→ зафиксированы свежие изменения"
fi

# 1. репозиторий (если уже есть — просто привязываем и пушим)
if gh repo view "$FULL" >/dev/null 2>&1; then
  echo "репозиторий уже существует — пушу"
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$FULL.git"
  git push -u origin main
else
  gh repo create "$REPO_NAME" --public --source=. --remote=origin --push
fi
echo "📦 https://github.com/$FULL"

# 2. секреты
bash scripts/set-github-secrets.sh "$FULL"

# 3. запустить workflow'ы
gh workflow run reports.yml -R "$FULL" -f mode=evening && echo "▶️ сводка запущена"
sleep 2
gh workflow run bot.yml -R "$FULL" && echo "▶️ бот запущен"

echo "✅ Готово. Логи: https://github.com/$FULL/actions"
