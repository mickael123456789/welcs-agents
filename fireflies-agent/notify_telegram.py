"""
Отправка отчёта в Telegram через Bot API.

Нужны два значения в .env:
  TELEGRAM_BOT_TOKEN  — токен от @BotFather
  TELEGRAM_CHAT_ID    — ваш chat_id (узнаётся через get_chat_id ниже)

Telegram ограничивает сообщение 4096 символами — длинный отчёт бьётся на части.
Поддерживается ограниченный HTML (<b>, <i>, <a>, <code>) — markdown отчёта
конвертируется в него.
"""

from __future__ import annotations

import html
import re
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3900  # запас от лимита 4096


def _md_to_tg_html(text: str) -> str:
    """Лёгкая конвертация markdown отчёта в Telegram-HTML."""
    out_lines = []
    for line in text.split("\n"):
        raw = line.rstrip()
        # экранируем спецсимволы HTML
        esc = html.escape(raw)
        # заголовки markdown (#, ##, ###) -> жирная строка
        m = re.match(r"^#{1,6}\s+(.*)$", esc)
        if m:
            esc = f"<b>{m.group(1)}</b>"
        else:
            # **жирный** -> <b>
            esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            # *курсив* / _курсив_ -> <i>
            esc = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<i>\1</i>", esc)
        out_lines.append(esc)
    return "\n".join(out_lines)


def _split(text: str, limit: int = LIMIT) -> list[str]:
    """Бьём текст на куски <= limit, по границам абзацев/строк."""
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        piece = para + "\n"
        if len(buf) + len(piece) > limit:
            if buf:
                chunks.append(buf.rstrip())
            # сам абзац длиннее лимита — режем жёстко
            while len(piece) > limit:
                chunks.append(piece[:limit])
                piece = piece[limit:]
            buf = piece
        else:
            buf += piece
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks or [text[:limit]]


def send_telegram(report: str, cfg: dict, header: str | None = None) -> None:
    token = cfg["TELEGRAM_BOT_TOKEN"]
    chat_id = cfg["TELEGRAM_CHAT_ID"]
    body = (_md_to_tg_html(header) + "\n\n" if header else "") + _md_to_tg_html(report)

    for i, chunk in enumerate(_split(body)):
        resp = requests.post(
            API.format(token=token, method="sendMessage"),
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        data = resp.json()
        if not data.get("ok"):
            # если HTML не распарсился — повторяем без разметки
            resp = requests.post(
                API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": re.sub(r"<[^>]+>", "", chunk),
                      "disable_web_page_preview": True},
                timeout=30,
            )
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram error: {data}")
        time.sleep(0.4)  # бережём лимит на частые сообщения


def get_chat_id(token: str) -> list[dict]:
    """
    Утилита: показывает, кто недавно писал боту, и их chat_id.
    Сначала напишите боту любое сообщение, потом запустите эту функцию.
    """
    resp = requests.get(API.format(token=token, method="getUpdates"), timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    seen = []
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat:
            seen.append({
                "chat_id": chat.get("id"),
                "name": " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                        or chat.get("title") or chat.get("username"),
                "type": chat.get("type"),
            })
    # уникальные
    uniq = {c["chat_id"]: c for c in seen}
    return list(uniq.values())


if __name__ == "__main__":
    # быстрый способ узнать chat_id: python notify_telegram.py <TOKEN>
    import sys
    if len(sys.argv) < 2:
        print("Использование: python notify_telegram.py <BOT_TOKEN>")
        raise SystemExit(1)
    for c in get_chat_id(sys.argv[1]):
        print(f"  chat_id={c['chat_id']}  | {c['name']}  ({c['type']})")
