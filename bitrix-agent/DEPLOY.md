# 🚀 Развёртывание на GitHub Actions (запуск 2 раза в день, бесплатно)

Агент будет сам запускаться **утром (08:00)** и **вечером (20:00)** в облаке GitHub
и слать сводку в Telegram. Mac включать не нужно.

## Шаг 1. Создать приватный репозиторий

1. На github.com → **New repository** → имя, например `bitrix-watch` → **Private** → Create.
2. Локально (папка `bitrix-agent` = корень репозитория):

```bash
cd bitrix-agent
git init
git add .
git commit -m "Bitrix daily report agent"
git branch -M main
git remote add origin https://github.com/ВАШ_ЛОГИН/bitrix-watch.git
git push -u origin main
```

> ⚠️ Файл `.env` НЕ попадёт в репозиторий (он в `.gitignore`) — и правильно.
> Секреты передаём через GitHub Secrets (шаг 2), а не через файл.

## Шаг 2. Добавить секреты

В репозитории: **Settings → Secrets and variables → Actions → New repository secret**.
Создайте 4 секрета:

| Имя | Значение |
|---|---|
| `BITRIX_WEBHOOK_URL` | `https://welcs.bitrix24.eu/rest/1102/…/` |
| `ANTHROPIC_API_KEY` | ваш ключ Claude (`sk-ant-…`) |
| `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | `1168080351` |

## Шаг 3. Проверить вручную

**Actions → Bitrix daily report → Run workflow** (кнопка справа) → выберите `morning` → Run.
Через минуту в Telegram должна прийти сводка. Логи запуска видны там же в Actions.

## Готово

Дальше всё само: каждый день в 08:00 и 20:00 (время Испании, лето). Чтобы поменять
часы — отредактируйте `cron` в `.github/workflows/bitrix-report.yml` (время в UTC).

---

## Если что-то не пришло
- **Actions → последний запуск → откройте лог** шага «Run agent» — там видно ошибку.
- Частые причины: не добавлен секрет, истёк ключ Claude, превышены лимиты Bitrix.
- Отчёт всё равно сохраняется в `reports/` внутри запуска (artifact можно не хранить).

## Стоимость
- GitHub Actions: бесплатно (приватный репо — 2000 минут/мес, запуск ~1–2 мин).
- Claude API: ~один вызов Opus на запуск, 2 запуска/день. Недорого; при желании
  поставьте `MODEL=claude-sonnet-4-6` в workflow для экономии.
