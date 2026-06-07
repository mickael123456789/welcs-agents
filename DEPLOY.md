# 🚀 Развёртывание в облаке (GitHub Actions) — бесплатно, без твоего компьютера

После этих шагов и сводки (☀️🌙), и живой бот будут работать в облаке GitHub
независимо от того, включён ли Mac.

---

## Шаг 1. Создать приватный репозиторий

1. Зайди на **github.com → New repository**.
2. Имя: `welcs-agents`. Видимость: **Private**. Нажми **Create repository**.
3. НЕ добавляй README/.gitignore (они уже есть в проекте).

## Шаг 2. Залить код

Я уже инициализировал git и сделал первый коммит. Осталось привязать твой репозиторий
и запушить. В терминале (подставь свой логин вместо `ВАШ_ЛОГИН`):

```bash
cd ~/welcs-agents
git remote add origin https://github.com/ВАШ_ЛОГИН/welcs-agents.git
git branch -M main
git push -u origin main
```

> 🔒 Файлы `.env` с ключами **не** попадают в репозиторий (они в `.gitignore`).
> Секреты добавим отдельно на шаге 3 — это правильно и безопасно.

## Шаг 3. Добавить секреты

GitHub → твой репозиторий → **Settings → Secrets and variables → Actions →
New repository secret**. Создай эти секреты (значения уже лежат в твоих локальных
`.env` — `~/welcs-agents/fireflies-agent/.env` и `.../bitrix-agent/.env`):

| Имя секрета | Где взять / значение |
|---|---|
| `FIREFLIES_API_KEY` | app.fireflies.ai → Settings → Developer Settings |
| `ANTHROPIC_API_KEY` | console.anthropic.com (`sk-ant-…`) |
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | `1168080351` |
| `BITRIX_WEBHOOK_URL` | `https://welcs.bitrix24.eu/rest/1102/…/` |
| `NOTION_API_KEY` | notion.so/my-integrations (для режима 🎯 Цели) |

Необязательные (есть значения по умолчанию в коде):
`ME_EMAILS`, `TEAM_DOMAINS`, `TEAM_EMAILS`.

> 💡 **Быстрый способ** добавить все секреты одной командой — см. шаг 3-bis ниже.

### Шаг 3-bis (опц.). Залить секреты автоматически

Если установить GitHub CLI, скрипт зальёт все секреты из твоих `.env`:

```bash
brew install gh         # установить GitHub CLI
gh auth login           # войти (выбрать GitHub.com → браузер)
cd ~/welcs-agents
bash scripts/set-github-secrets.sh ВАШ_ЛОГИН/welcs-agents
```

## Шаг 4. Включить и проверить

1. В репозитории открой вкладку **Actions** → если просит — нажми
   «I understand my workflows, go ahead and enable them».
2. **Daily reports** → **Run workflow** → выбери `evening` → Run. Через минуту в
   Telegram придёт вечерняя сводка. Если пришла — расписание работает.
3. **Telegram bot (24/7 keep-alive)** → **Run workflow** → Run. Бот станет живым в
   облаке. Открой Telegram, нажми кнопку (🔥/📊/🎯) и задай вопрос.

## Шаг 5. Выключить локальные демоны на Mac

Когда облачный бот ожил — **локальный бот на Mac надо остановить**, иначе два
слушателя на один токен будут конфликтовать (Telegram отдаёт ошибку 409). Скажи мне
«облако работает», и я выключу локальные демоны. Или вручную:

```bash
launchctl bootout gui/$(id -u)/com.welcs.fireflies-bot
launchctl bootout gui/$(id -u)/com.welcs.fireflies-agent
```

---

## ⚡ Чтобы бот отвечал без пауз (рекомендуется)

GitHub ограничивает один запуск ~6 часами. Без доп. настройки бот перезапускается
по расписанию каждые 6 ч — раз в ~6 ч возможна пауза в несколько минут.

Чтобы перезапуск был **мгновенным**, добавь токен:
1. github.com → **Settings (профиль) → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
2. Repository access: только `welcs-agents`. Permissions → **Actions: Read and write**.
3. Скопируй токен и добавь как секрет репозитория с именем **`BOT_PAT`**.

После этого бот сам перезапускает себя за секунды — пауз практически нет.

---

## Время отправки сводок

В `.github/workflows/reports.yml` время указано в **UTC**. Сейчас стоит:
`07:00 UTC` (≈09:00 по Испании летом) и `18:00 UTC` (≈20:00). Зимой сдвинется на час —
поправь `cron`, если нужно.

## Стоимость

- **GitHub Actions:** приватный репо — 2000 бесплатных минут/мес. Бот «съедает» минуты,
  пока работает (≈живёт постоянно). Если упрёшься в лимит — можно сделать репо
  **публичным** (тогда минуты Actions безлимитны) или вынести бота на Fly.io.
- **Claude API:** оплата по использованию (вопросы к боту + 2 сводки/день).

> ⚠️ **Важно про лимит минут:** живой бот на приватном репо может выбрать 2000 мин за
> ~3 дня. Варианты: сделать репозиторий **public** (Actions-минуты бесплатны и
> безлимитны для public; секреты при этом остаются скрытыми), либо перенести только
> бота на Fly.io/VPS. Сводки (☀️🌙) в минуты почти не упираются.
