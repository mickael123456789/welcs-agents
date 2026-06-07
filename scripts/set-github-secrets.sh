#!/usr/bin/env bash
# Заливает секреты из локальных .env в GitHub Actions одной командой.
# Требует установленного и авторизованного GitHub CLI (gh auth login).
#
#   bash scripts/set-github-secrets.sh ВАШ_ЛОГИН/welcs-agents
set -euo pipefail

REPO="${1:?Использование: bash scripts/set-github-secrets.sh ВАШ_ЛОГИН/welcs-agents}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

command -v gh >/dev/null || { echo "❌ Нет gh. Установи: brew install gh && gh auth login"; exit 1; }

# Значение ключа из любого из двух .env (fireflies перекрывает bitrix).
get() { grep -hE "^$1=" "$DIR/bitrix-agent/.env" "$DIR/fireflies-agent/.env" 2>/dev/null | tail -1 | cut -d= -f2-; }

for KEY in FIREFLIES_API_KEY ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID \
           BITRIX_WEBHOOK_URL NOTION_API_KEY ME_EMAILS TEAM_DOMAINS TEAM_EMAILS; do
  VAL="$(get "$KEY" || true)"
  if [ -n "${VAL:-}" ]; then
    printf '%s' "$VAL" | gh secret set "$KEY" -R "$REPO"   # значение читается со stdin
    echo "✓ $KEY"
  else
    echo "– $KEY (нет в .env — пропускаю)"
  fi
done
echo "Готово. Проверь: Settings → Secrets and variables → Actions."
