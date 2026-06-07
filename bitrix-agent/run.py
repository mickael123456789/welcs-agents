#!/usr/bin/env python3
"""
Ежедневный надзорный агент по Bitrix24.

Пайплайн:  сбор данных  →  анализ Claude  →  письмо на почту  →  лог в файл.

Запуск:
    python run.py              # полный цикл (сбор + анализ + письмо)
    python run.py --dry        # без отправки письма, печать отчёта в консоль
    python run.py --no-email   # сохранить отчёт в файл, но не слать письмо

Конфигурация — через переменные окружения (см. .env.example) или файл .env.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPORTS_DIR = HERE / "reports"


def load_env() -> dict:
    """Читаем .env (если есть) + переменные окружения."""
    env: dict[str, str] = {}
    dotenv = HERE / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    # переменные окружения имеют приоритет
    for k in ("BITRIX_WEBHOOK_URL", "ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_PORT",
              "SMTP_USER", "SMTP_PASSWORD", "REPORT_TO", "MODEL", "LOOKBACK_HOURS",
              "MAX_DIALOGS", "MSGS_PER_DIALOG", "MAX_CRM", "MAX_TASKS",
              "NOTIFY_CHANNEL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def as_int(env: dict, key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (ValueError, TypeError):
        return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="не отправлять, печать в консоль")
    ap.add_argument("--no-email", action="store_true", help="сохранить в файл, без отправки")
    ap.add_argument("--save-raw", action="store_true", help="сохранить сырую выгрузку JSON")
    ap.add_argument("--mode", choices=["morning", "evening"], help="режим: утро/вечер")
    args = ap.parse_args()

    env = load_env()
    if not env.get("BITRIX_WEBHOOK_URL"):
        print("❌ Не задан BITRIX_WEBHOOK_URL (.env или переменная окружения).", file=sys.stderr)
        return 2
    if not env.get("ANTHROPIC_API_KEY") and not args.save_raw:
        print("❌ Не задан ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    from bitrix_client import Bitrix
    from collect import collect_all

    mode = args.mode or (env.get("MODE") or "morning").lower()
    # окно по умолчанию зависит от режима: утром смотрим ночь, вечером — день
    default_lookback = 14 if mode == "morning" else 12
    cfg = {
        "LOOKBACK_HOURS": as_int(env, "LOOKBACK_HOURS", default_lookback),
        "MAX_DIALOGS": as_int(env, "MAX_DIALOGS", 40),
        "MSGS_PER_DIALOG": as_int(env, "MSGS_PER_DIALOG", 30),
        "MAX_CRM": as_int(env, "MAX_CRM", 60),
        "MAX_TASKS": as_int(env, "MAX_TASKS", 50),
    }

    print("→ Подключаюсь к Bitrix и собираю данные…")
    bx = Bitrix(env["BITRIX_WEBHOOK_URL"])
    data = collect_all(bx, cfg)
    print(f"  открытые линии: {len(data['open_lines'])} | "
          f"внутр. чаты: {len(data['team_chats'])} | "
          f"сделки: {len(data['deals'])} | лиды: {len(data['leads'])} | "
          f"просроч. задачи: {len(data['overdue_tasks'])}")
    for err in (data.get("_collect_errors") or []):
        print(f"  ⚠️ {err}")

    REPORTS_DIR.mkdir(exist_ok=True)
    today = dt.date.today().isoformat()

    if args.save_raw:
        raw_path = REPORTS_DIR / f"raw-{today}.json"
        raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  сырые данные → {raw_path}")
        if not env.get("ANTHROPIC_API_KEY"):
            return 0

    print(f"→ Считаю метрики (SLA, операторы, готовность)… режим: {mode}")
    from analytics import compute
    metrics = compute(data)
    print(f"  без ответа: {metrics['sla']['unanswered_count']} | "
          f"уборки не сделаны сегодня/завтра: "
          f"{len(metrics['readiness']['today'])}/{len(metrics['readiness']['tomorrow'])} | "
          f"инциденты: {len(metrics['incidents'])} | составные кейсы: {len(metrics['linked_cases'])}")

    print("→ Анализирую через Claude…")
    from analyze import analyze
    report = analyze(data, env["ANTHROPIC_API_KEY"], env.get("MODEL", "claude-opus-4-8"),
                     mode=mode, metrics=metrics)

    report_path = REPORTS_DIR / f"report-{today}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"  отчёт → {report_path}")

    if args.dry:
        print("\n" + "=" * 60 + "\n" + report + "\n" + "=" * 60)
        return 0

    if args.no_email:
        return 0

    channel = (env.get("NOTIFY_CHANNEL") or "telegram").lower()
    emoji = "☀️ Утренняя" if mode == "morning" else "🌙 Вечерняя"
    title = f"{emoji} сводка по Bitrix — {today}"

    if channel == "telegram":
        print("→ Отправляю в Telegram…")
        from notify_telegram import send_telegram
        try:
            send_telegram(report, env, header=title)
            print(f"  ✅ Отправлено в Telegram (chat_id {env.get('TELEGRAM_CHAT_ID')})")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Не удалось отправить в Telegram: {e}", file=sys.stderr)
            print(f"  Отчёт сохранён в {report_path}", file=sys.stderr)
            return 1
    else:
        print("→ Отправляю письмо…")
        from notify import send_email
        try:
            send_email(report, env, title)
            print(f"  ✅ Письмо отправлено на {env.get('REPORT_TO', env.get('SMTP_USER'))}")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Не удалось отправить письмо: {e}", file=sys.stderr)
            print(f"  Отчёт сохранён в {report_path}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
