#!/usr/bin/env python3
"""
Утренний «пинок»: шлёт в Telegram приглашение к прогону дня.
Запускается планировщиком (GitHub Actions) в 09:00 — пользователь жмёт 🌅 Утро
и присылает план дня, дальше отвечает живой бот.
"""

from __future__ import annotations

import sys

import requests

from bot import _read_env
from morning import NUDGE

TG = "https://api.telegram.org/bot{token}/sendMessage"


def main() -> int:
    env = _read_env()
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not env.get(k):
            print(f"❌ Не задан {k}", file=sys.stderr)
            return 2
    r = requests.post(
        TG.format(token=env["TELEGRAM_BOT_TOKEN"]),
        json={"chat_id": env["TELEGRAM_CHAT_ID"], "text": NUDGE,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    ).json()
    if not r.get("ok"):
        print(f"❌ Telegram: {r}", file=sys.stderr)
        return 1
    print("✅ утренний пинок отправлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
