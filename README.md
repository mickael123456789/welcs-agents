# Welcs agents — Telegram-бот с 4 интеграциями

Единый Telegram-бот (@AuditorWelcs_bot) и ежедневные сводки. Работают в облаке
(GitHub Actions), **не зависят от того, включён ли компьютер.**

## Что внутри

| Режим / задача | Что делает | Интеграции |
|---|---|---|
| 🔥 **Fireflies** | вопросы о встречах команды и собеседованиях | Fireflies + Claude |
| 📊 **Bitrix** | вопросы по CRM, сделкам, задачам, чатам | Bitrix24 + Claude |
| 🎯 **Цели** | мысль/идея/задача → разбор по целям → запись | Notion + Bitrix + Claude |
| 🧠 **Совет** | совет/решение по вопросу | Claude (+ Notion) |
| ☀️🌙 **Сводки** | авто-отчёты утром и вечером в Telegram | Fireflies + Bitrix + Claude |

## Структура

```
welcs-agents/
├── fireflies-agent/      # бот (bot.py) + сводки по встречам (run.py) + Цели/Совет
├── bitrix-agent/         # ежедневные сводки по Bitrix24 (run.py)
├── .github/workflows/
│   ├── reports.yml        # ☀️🌙 сводки 2 раза в день (cron)
│   └── bot.yml            # 🤖 живой бот 24/7 (self-restart)
├── requirements.txt
└── DEPLOY.md             # ← пошаговая инструкция развёртывания
```

## Развёртывание

См. **[DEPLOY.md](DEPLOY.md)** — создать репозиторий, добавить секреты, включить workflow.

## Локальный запуск (для отладки)

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cd fireflies-agent
cp .env.example .env   # вписать ключи
python run.py --mode evening --dry   # сводка в консоль
python bot.py                        # живой бот
```
