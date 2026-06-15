# Fireflies daily agent

Ежедневный надзорный агент по встречам команды. Каждый день ходит в **Fireflies**,
читает все записанные встречи (внутренние, собеседования с кандидатами, внешние —
партнёры/инвесторы/клиенты), прогоняет через **Claude** и шлёт сводку в **Telegram**
(тот же бот, что у Bitrix-агента — @AuditorWelcs_bot).

## Что в отчёте

- 🔴 **Внимание руководителя** — что важно сейчас, риски для качества/репутации.
- ⏳ **Задержки и риски** — где команда буксует, переносит сроки, паттерны.
- 🧑‍💼 **Собеседования** — по кандидатам: впечатление, красные флаги, рекомендация.
- 👏 **Похвалить / 👎 внимание** — поимённо, с фактами.
- 🧭 **Пульс дня** + индекс внимания N/10.
- ⏰ **Дедлайны и обязательства** — из ТВОИХ встреч извлекаются договорённости
  («X пришлёт к пятнице», «Y отчитается завтра»), копятся в реестре и напоминаются
  **в день дедлайна**.

Два запуска в день:
- **09:00 (morning)** — напоминания о дедлайнах на сегодня + просрочка + короткий список встреч.
- **20:00 (evening)** — полная сводка дня.

## Файлы

| файл | что делает |
|------|-----------|
| `fireflies_client.py` | GraphQL-клиент Fireflies |
| `collect.py` | сбор встреч за период + классификация (команда / собес / внешняя) |
| `ledger.py` | реестр обязательств/дедлайнов (`commitments.json`) |
| `analyze.py` | Claude: извлечение обязательств (tool use) + сборка отчёта |
| `notify_telegram.py` | отправка в Telegram |
| `run.py` | оркестратор |
| `run_daily.sh` | обёртка для launchd (выбирает morning/evening по времени) |

## Настройка

1. Заполни `.env` (см. `.env.example`). Telegram уже прописан. Нужно добавить:
   - `FIREFLIES_API_KEY` — app.fireflies.ai → Settings → Developer Settings → API Key
   - `ANTHROPIC_API_KEY` — console.anthropic.com
2. Проверка вручную:
   ```bash
   ../bitrix-agent/.venv/bin/python run.py --mode evening --dry   # печать в консоль
   ../bitrix-agent/.venv/bin/python run.py --mode evening         # отправить в Telegram
   ```

## Расписание (launchd)

Установлено `~/Library/LaunchAgents/com.welcs.fireflies-agent.plist` (09:00 и 20:00).
launchd, в отличие от cron, догоняет пропущенный запуск после сна/включения Mac.

```bash
# перезагрузить после правок plist:
launchctl bootout  gui/$(id -u)/com.welcs.fireflies-agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.welcs.fireflies-agent.plist
# разовый прогон прямо сейчас:
launchctl kickstart -k gui/$(id -u)/com.welcs.fireflies-agent
# логи:
tail -f reports/agent.log
```

> ⚠️ Mac должен быть **включён** в момент запуска (или проснуться позже — launchd догонит).
> Для запуска независимо от ноутбука агента можно перенести на всегда-включённый хост.

## Диалоговый режим (вопросы боту)

Кроме ежедневной рассылки бот умеет **отвечать на вопросы**. Пишешь ему в Telegram
(«что обсуждали по дашборду на этой неделе?», «как прошло собеседование с Дианой?») —
он ищет нужные встречи в Fireflies, при необходимости читает стенограмму целиком и отвечает.

Поиск идёт **по всей истории** Fireflies (по теме/названию встречи или участнику),
а не только по последним встречам — инструмент `search_meetings`. Для вопросов про
недавние встречи без конкретной темы используется `list_recent_meetings` (до 365 дней).

Это постоянно работающий процесс `bot.py` (демон `com.welcs.fireflies-bot`, KeepAlive).
Бот **приватный**: отвечает только владельцу (`TELEGRAM_CHAT_ID`), всем остальным —
вежливый отказ.

```bash
# запустить демон (ПОСЛЕ того как заполнены ключи в .env):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.welcs.fireflies-bot.plist
# проверить вручную в консоли:
../bitrix-agent/.venv/bin/python bot.py
# логи:
tail -f reports/bot.out.log reports/bot.err.log
# остановить:
launchctl bootout gui/$(id -u)/com.welcs.fireflies-bot
```
