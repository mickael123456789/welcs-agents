#!/usr/bin/env python3
"""
Ежедневный надзорный агент по встречам команды (Fireflies → Claude → Telegram).

Пайплайн:  сбор встреч  →  извлечение обязательств в реестр  →  анализ Claude  →  Telegram.

Запуск:
    python run.py --mode evening      # вечерняя сводка дня (по умолчанию)
    python run.py --mode morning      # утренние напоминания о дедлайнах на сегодня
    python run.py --dry               # не слать в Telegram, печать в консоль
    python run.py --save-raw          # сохранить сырую выгрузку JSON

Конфигурация — переменные окружения или файл .env (см. .env.example).
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
LEDGER_PATH = HERE / "commitments.json"

_KEYS = (
    "FIREFLIES_API_KEY", "ANTHROPIC_API_KEY", "MODEL",
    "NOTIFY_CHANNEL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "ME_EMAILS", "TEAM_DOMAINS", "TEAM_EMAILS",
    "LOOKBACK_HOURS", "MAX_MEETINGS", "UPCOMING_HORIZON_DAYS",
)


def load_env() -> dict:
    env: dict[str, str] = {}
    dotenv = HERE / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in _KEYS:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def as_int(env: dict, key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (ValueError, TypeError):
        return default


def split_set(val: str | None) -> set[str]:
    return {x.strip().lower() for x in (val or "").replace(";", ",").split(",") if x.strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["morning", "evening"], default="evening")
    ap.add_argument("--dry", action="store_true", help="не слать в Telegram, печать в консоль")
    ap.add_argument("--save-raw", action="store_true", help="сохранить сырую выгрузку JSON")
    args = ap.parse_args()

    env = load_env()
    if not env.get("FIREFLIES_API_KEY"):
        print("❌ Не задан FIREFLIES_API_KEY (.env или переменная окружения).", file=sys.stderr)
        return 2
    if not env.get("ANTHROPIC_API_KEY"):
        print("❌ Не задан ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    from fireflies_client import Fireflies
    from collect import collect_all, DEFAULT_TEAM_DOMAINS, DEFAULT_TEAM_EMAILS
    import ledger

    # утром смотрим на сутки назад (свежие встречи), но дедлайны берём из всего реестра
    default_lookback = 24
    cfg = {
        "ME_EMAILS": split_set(env.get("ME_EMAILS")) or {"mo@welcs.com", "mo@welcs.app"},
        "TEAM_DOMAINS": split_set(env.get("TEAM_DOMAINS")) or set(DEFAULT_TEAM_DOMAINS),
        "TEAM_EMAILS": split_set(env.get("TEAM_EMAILS")) | set(DEFAULT_TEAM_EMAILS),
        "LOOKBACK_HOURS": as_int(env, "LOOKBACK_HOURS", default_lookback),
        "MAX_MEETINGS": as_int(env, "MAX_MEETINGS", 50),
    }
    model = env.get("MODEL", "claude-opus-4-8")
    today = dt.date.today().isoformat()

    print(f"→ Режим: {args.mode}. Собираю встречи за {cfg['LOOKBACK_HOURS']} ч…")
    ff = Fireflies(env["FIREFLIES_API_KEY"])
    data = collect_all(ff, cfg)
    c = data["counts"]
    print(f"  встречи: {len(data['meetings'])} (внутр {c['internal']} / собес {c['interview']} / "
          f"внешн {c['external']}); мои: {c['my_meetings']}")
    for err in (data.get("_collect_errors") or []):
        print(f"  ⚠️ {err}")

    REPORTS_DIR.mkdir(exist_ok=True)
    if args.save_raw:
        raw = REPORTS_DIR / f"raw-{today}-{args.mode}.json"
        raw.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  сырые данные → {raw}")

    # ── обновляем реестр обязательств из новых встреч ───────────────────────────
    items = ledger.load(LEDGER_PATH)
    title_by_id = {m["id"]: m["title"] for m in data["meetings"]}
    date_by_id = {m["id"]: m["date"] for m in data["meetings"]}
    try:
        from analyze import extract_commitments
        extracted = extract_commitments(data["meetings"], env["ANTHROPIC_API_KEY"], model, today)
        for e in extracted:
            e["source_title"] = title_by_id.get(e.get("source_id"))
            e["source_date"] = date_by_id.get(e.get("source_id"))
        items = ledger.merge(items, extracted, today)
        ledger.save(LEDGER_PATH, items)
        print(f"  обязательств в реестре: {len(items)} (+{len(extracted)} из сегодняшних встреч)")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ Извлечение обязательств не удалось: {e}", file=sys.stderr)

    horizon = as_int(env, "UPCOMING_HORIZON_DAYS", 3)
    buckets = ledger.due_buckets(items, today, horizon_days=horizon)

    # ── собираем отчёт ─────────────────────────────────────────────────────────
    print("→ Готовлю отчёт через Claude…")
    from analyze import build_report
    report = build_report(data, buckets, env["ANTHROPIC_API_KEY"], model, args.mode)

    report_path = REPORTS_DIR / f"report-{today}-{args.mode}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"  отчёт → {report_path}")

    if args.dry:
        print("\n" + "=" * 60 + "\n" + report + "\n" + "=" * 60)
        return 0

    title = (f"🌅 Команда — утро {today}" if args.mode == "morning"
             else f"🌙 Сводка по команде — {today}")
    print("→ Отправляю в Telegram…")
    from notify_telegram import send_telegram
    try:
        send_telegram(report, env, header=title)
        print(f"  ✅ Отправлено в Telegram (chat_id {env.get('TELEGRAM_CHAT_ID')})")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Не удалось отправить в Telegram: {e}", file=sys.stderr)
        print(f"  Отчёт сохранён в {report_path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
