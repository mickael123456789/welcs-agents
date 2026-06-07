"""
Анализ встреч через Claude.

Две задачи:
  1. extract_commitments() — из встреч руководителя достаёт обязательства/дедлайны
     в структурированном виде (через tool use) для реестра.
  2. build_report() — собирает читаемую сводку в Telegram (утренний/вечерний режим).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import anthropic

# ── 1. Извлечение обязательств ────────────────────────────────────────────────

_COMMIT_TOOL = {
    "name": "record_commitments",
    "description": "Зафиксировать обязательства и дедлайны, прозвучавшие на встречах.",
    "input_schema": {
        "type": "object",
        "properties": {
            "commitments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "who": {"type": "string", "description": "Кто отвечает за выполнение (имя)."},
                        "what": {"type": "string", "description": "Что именно должен сделать/прислать."},
                        "due_date": {
                            "type": "string",
                            "description": "Дедлайн в формате YYYY-MM-DD. Пустая строка, если срок не назван.",
                        },
                        "to_me": {
                            "type": "boolean",
                            "description": "True, если это обещание/задача ПЕРЕД руководителем (он ждёт отчёта/результата).",
                        },
                        "source_id": {"type": "string", "description": "id встречи-источника."},
                    },
                    "required": ["who", "what", "due_date", "to_me", "source_id"],
                },
            }
        },
        "required": ["commitments"],
    },
}

_COMMIT_SYSTEM = """\
Ты разбираешь стенограммы встреч руководителя и достаёшь из них конкретные \
обязательства с дедлайнами: кто, что должен сделать и к какой дате.

Правила:
- Бери только реальные договорённости и обещания (action items, «я пришлю к пятнице», \
  «сделаю до конца недели», «отчитаюсь завтра»). Не выдумывай.
- Дату вычисляй абсолютной (YYYY-MM-DD) относительно даты встречи. «к пятнице», \
  «завтра», «до конца недели», «через неделю» — переводи в конкретную дату. \
  Если срок не назван вовсе — оставь due_date пустым.
- to_me=true, если результат/отчёт ждёт именно руководитель (Mickael / Михаил / Mikhail \
  Ostrovskiy, mo@welcs.com). Иначе false.
- source_id — id той встречи, откуда взято обязательство.
- Если обязательств нет — верни пустой список.
Всегда вызывай инструмент record_commitments."""


def extract_commitments(
    meetings: list[dict[str, Any]],
    api_key: str,
    model: str,
    today: str,
) -> list[dict[str, Any]]:
    """Возвращает список обязательств (без id/служебных полей — их добавит ledger)."""
    from ledger import make_id

    # анализируем только встречи, где был руководитель
    mine = [m for m in meetings if m.get("me_present")]
    if not mine:
        return []

    payload = json.dumps(
        [{
            "source_id": m["id"],
            "title": m["title"],
            "date": m["date"],
            "summary": m["summary"].get("short_summary"),
            "action_items": m["summary"].get("action_items"),
        } for m in mine],
        ensure_ascii=False,
    )

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=[{"type": "text", "text": _COMMIT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[_COMMIT_TOOL],
        tool_choice={"type": "tool", "name": "record_commitments"},
        messages=[{
            "role": "user",
            "content": f"Сегодня {today}. Встречи руководителя:\n```json\n{payload}\n```",
        }],
    )

    out: list[dict[str, Any]] = []
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_commitments":
            for c in block.input.get("commitments", []):
                who, what, src = c.get("who", "").strip(), c.get("what", "").strip(), c.get("source_id", "")
                if not who or not what:
                    continue
                out.append({
                    "id": make_id(who, what, src),
                    "who": who,
                    "what": what,
                    "due_date": (c.get("due_date") or "").strip() or None,
                    "to_me": bool(c.get("to_me")),
                    "source_id": src,
                })
    return out


# ── 2. Сборка отчёта ──────────────────────────────────────────────────────────

_REPORT_SYSTEM = """\
Ты — внимательный операционный директор. Каждый день ты просматриваешь все встречи \
команды (внутренние, с кандидатами на собеседованиях, с партнёрами/инвесторами/клиентами), \
записанные в Fireflies, и готовишь руководителю краткую честную сводку в Telegram.

Пиши по-русски, по-деловому, плотно. Руководитель читает это за 2–3 минуты.
Используй только то, что подтверждается данными встреч. Не выдумывай. Если данных мало — \
скажи прямо. Имена бери как есть из участников/action items.

Структура отчёта (опускай пустые разделы, не пиши «ничего»):

🔴 ВНИМАНИЕ РУКОВОДИТЕЛЯ
   Что важно сейчас: риск для качества/репутации компании, буксующий проект, \
   конфликт, сорванная договорённость, решение, принятое без тебя, которое стоит \
   перепроверить. Для каждого пункта — суть в 1–2 строки и КОНКРЕТНОЕ действие.

⏳ ЗАДЕРЖКИ И РИСКИ
   Где команда тормозит, откладывает, переносит сроки; что повторяется из встречи \
   в встречу; решения, способные ударить по качеству. Паттерны, а не разовые мелочи.

🧑‍💼 СОБЕСЕДОВАНИЯ
   По каждому кандидату: имя, на какую роль, общее впечатление, красные флаги, \
   рекомендация (брать / подумать / нет). Только если были встречи-собеседования.

👏 ПОХВАЛИТЬ  /  👎 ОБРАТИТЬ ВНИМАНИЕ
   Поимённо: кто продвинул дело, проявил инициативу — и кто сорвал срок, проигнорил, \
   дал неверную инфу. С фактами. Будь справедлив: если контекст оправдывает — отметь.

🧭 ПУЛЬС ДНЯ
   2–3 строки: чем жила команда, общий тон, тренд.

В самом конце — строка «ИНДЕКС ВНИМАНИЯ: N/10» (0 — всё спокойно, 10 — пожар)."""


def _fmt_due_section(buckets: dict[str, list]) -> str:
    """Готовый текст по дедлайнам (детерминированно, без обращения к модели)."""
    def line(c: dict) -> str:
        who = c.get("who", "?")
        what = c.get("what", "")
        dd = c.get("due_date") or "—"
        tag = " ⟵ ждёшь ты" if c.get("to_me") else ""
        src = f" ({c.get('source_title')})" if c.get("source_title") else ""
        return f"• **{dd}** — {who}: {what}{tag}{src}"

    parts: list[str] = []
    if buckets["overdue"]:
        parts.append("‼️ **Просрочено:**\n" + "\n".join(line(c) for c in buckets["overdue"]))
    if buckets["due_today"]:
        parts.append("📌 **Сегодня дедлайн:**\n" + "\n".join(line(c) for c in buckets["due_today"]))
    if buckets["upcoming"]:
        parts.append("🗓 **Ближайшие дни:**\n" + "\n".join(line(c) for c in buckets["upcoming"]))
    return "\n\n".join(parts)


def build_report(
    data: dict[str, Any],
    buckets: dict[str, list],
    api_key: str,
    model: str,
    mode: str,
) -> str:
    """mode: 'morning' (напоминания о дедлайнах + короткий обзор) или 'evening' (полная сводка)."""
    today = dt.date.today().isoformat()
    due_text = _fmt_due_section(buckets)

    if mode == "morning":
        # Утром главное — дедлайны на сегодня и просрочка. Обзор встреч короткий.
        meetings = data.get("meetings", [])
        if not meetings and not due_text:
            return "🌅 Доброе утро. Новых встреч за сутки нет, дедлайнов на сегодня тоже нет."
        head = "🌅 **Утренний фокус**\n\n"
        body = due_text or "На сегодня дедлайнов из встреч нет."
        if meetings:
            titles = "\n".join(f"• {m['title']}" for m in meetings[-8:])
            body += f"\n\n🗂 *Встречи за сутки ({len(meetings)}):*\n{titles}"
        return head + body

    # evening — полноценный анализ через Claude
    meetings = data.get("meetings", [])
    if not meetings:
        base = "🌙 За сегодня записанных встреч в Fireflies нет."
        return base + (("\n\n" + due_text) if due_text else "")

    payload = json.dumps(data, ensure_ascii=False, indent=1)
    if len(payload) > 120_000:
        payload = payload[:120_000]

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=[{"type": "text", "text": _REPORT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": (
                f"Встречи команды за последние {data.get('lookback_hours')} ч "
                f"(сегодня {today}). Счётчики: {json.dumps(data.get('counts'), ensure_ascii=False)}.\n\n"
                f"```json\n{payload}\n```\n\n"
                "Подготовь вечернюю сводку по структуре из инструкции."
            ),
        }],
    )
    report = "".join(b.text for b in resp.content if b.type == "text")

    if due_text:
        report += "\n\n— — —\n⏰ **Дедлайны и обязательства**\n\n" + due_text
    return report
