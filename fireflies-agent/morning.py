#!/usr/bin/env python3
"""
🌅 Утренний прогон задач — режим бота.

Каждое утро пользователь сам пишет, что планирует на день. Бот прогоняет каждую
задачу через его личные вопросы и связывает с целями (3 / 6 / 12 мес из Notion),
показывает, на чём сфокусироваться и что делегировать, и сохраняет прогон в Notion.

Личные вопросы прогона (рамка пользователя):
  • Это «хочу», «важно» или «нужно»?
  • Позволяет ли это реализовать моё собственное «хочу»?
  • Есть ли кайф в процессе — чтобы не оказаться на обочине?
  • Зачем я это делаю? На самом деле зачем?
  • Мне самому интересно этим заниматься (как внутреннему ребёнку) — или делегировать?
  • Как это приближает меня к целям на 3 / 6 / 12 месяцев?

Точка входа: morning_review(text, env). Если пользователь формулирует ЦЕЛЬ —
бот её записывает (record_goal). Иначе разбирает задачи и сохраняет прогон дня.
"""

from __future__ import annotations

import datetime as dt
import json
import re

import anthropic

SYSTEM = """\
Ты — личный коуч-ассистент основателя по утреннему прогону дня. Каждое утро он \
присылает тебе список того, что планирует делать. Твоя задача — провести каждую \
задачу через ЕГО личную рамку вопросов и связать с его целями (горизонты 3 / 6 / 12 \
месяцев), чтобы он не оказался «на обочине» — занятым не тем.

Для КАЖДОЙ задачи из списка дай компактный разбор ровно по этой рамке:
• <task>
  — Хочу / Важно / Нужно: что это по сути.
  — Реализует ли это моё «хочу»: да / частично / нет — одна фраза почему.
  — Кайф в процессе: да / нет — есть ли энергия и удовольствие, или это «обочина».
  — Зачем я это делаю, на самом деле: честная глубинная причина в одной фразе \
    (не отписка — копни: страх, эго, деньги, рост, миссия?).
  — Сам или делегировать: «делать самому» (если это его зона роста / внутренний \
    ребёнок горит) или «делегировать → кому» (если это не его и не двигает цели).
  — Цель: к какой цели (3/6/12 мес) ведёт и насколько сильно (🔥 сильно / ◐ средне / ﹒слабо).

После разбора всех задач добавь:
🧭 Фокус дня: 1–2 фразы — что главное, что отдать/выкинуть, чтобы день работал на цели.
И последней строкой строго: «ВЫРАВНИВАНИЕ: N/100» — насколько сегодняшний день в целом \
двигает к целям (0 — день мимо целей, 100 — точно в цель).

Правила:
- Пиши по-русски, тепло, но честно и прямо — без воды и лести. Коротко.
- Опирайся на его реальные цели (даны ниже). Если целей ещё нет — НЕ разбирай задачи, \
  а мягко предложи сначала задать цели на 3/6/12 мес (и если он их формулирует — запиши \
  через record_goal).
- Если в сообщении он формулирует ЦЕЛЬ (а не задачи дня) — вызови record_goal для каждой, \
  с правильным горизонтом. Не выдумывай горизонт — если не ясно, спроси одним вопросом.
- Не выдумывай задачи, которых он не писал."""

TOOLS = [
    {"name": "record_goal",
     "description": "Записать новую цель пользователя в Notion (горизонт 3 мес / 6 мес / 1 год).",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Формулировка цели, конкретно и измеримо."},
         "horizon": {"type": "string", "enum": ["3 мес", "6 мес", "1 год"]},
         "why": {"type": "string", "description": "Зачем эта цель — глубинный смысл (можно пусто)."}},
         "required": ["title", "horizon"]}},
]


def _goals_context(env) -> str:
    from notion_client import Notion
    try:
        rows = Notion(env["NOTION_API_KEY"]).query_goals()
    except Exception as e:  # noqa: BLE001
        return f"(не удалось прочитать цели: {e})"
    if not rows:
        return "ЦЕЛЕЙ ПОКА НЕТ."
    by = {"3 мес": [], "6 мес": [], "1 год": [], "—": []}
    for r in rows:
        by.get(r.get("Горизонт") or "—", by["—"]).append(r.get("Название") or "")
    parts = []
    for h in ("3 мес", "6 мес", "1 год", "—"):
        if by[h]:
            label = "Без горизонта" if h == "—" else h
            parts.append(f"[{label}] " + "; ".join(x for x in by[h] if x))
    return "ЦЕЛИ:\n" + "\n".join(parts)


def today() -> str:
    return dt.date.today().isoformat()


def _alignment(text: str) -> int | None:
    m = re.search(r"ВЫРАВНИВАНИЕ:\s*(\d{1,3})\s*/\s*100", text)
    return int(m.group(1)) if m else None


def morning_review(text: str, env: dict) -> str:
    """Прогон дня. Возвращает готовый текст для Telegram (и сам пишет в Notion)."""
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    goals = _goals_context(env)

    messages = [{"role": "user", "content":
                 f"Сегодня {today()}.\n\n{goals}\n\nЧто я сегодня планирую:\n{text}"}]
    recorded_goals: list[str] = []

    for _ in range(6):
        resp = client.messages.create(
            model=model, max_tokens=3000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS, messages=messages)

        if resp.stop_reason != "tool_use":
            out = "".join(b.text for b in resp.content if b.type == "text").strip()
            if recorded_goals:
                out = ("✅ Записал цели: " + "; ".join(recorded_goals) + "\n\n") + out
            else:
                # это разбор дня — сохраняем прогон в Notion
                _save(out, env)
            return out or "Не понял — напиши план дня списком."

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if b.name == "record_goal":
                try:
                    from notion_client import Notion
                    Notion(env["NOTION_API_KEY"]).create_goal(
                        title=b.input["title"], horizon=b.input.get("horizon"),
                        why=b.input.get("why"))
                    recorded_goals.append(f"{b.input['title']} ({b.input.get('horizon','')})")
                    out = "ok"
                except Exception as e:  # noqa: BLE001
                    out = f"ошибка записи цели: {e}"
            else:
                out = "неизвестный инструмент"
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})

    return "Долго думаю — попробуй короче."


def _save(review_text: str, env: dict) -> None:
    """Сохраняет утренний прогон дня в Notion (не валит ответ при ошибке)."""
    try:
        from notion_client import Notion
        Notion(env["NOTION_API_KEY"]).create_day_plan(
            date=today(), body_md=review_text, alignment=_alignment(review_text))
    except Exception:  # noqa: BLE001
        pass


# Текст утреннего «пинка» (шлёт планировщик в 09:00)
NUDGE = ("🌅 <b>Доброе утро!</b>\n\nНажми <b>🌅 Утро</b> и напиши, что сегодня в планах — "
         "прогоню каждую задачу по твоим вопросам (хочу/важно/нужно, кайф, зачем на самом деле, "
         "сам или делегировать) и сверю с целями на 3/6/12 мес.")
