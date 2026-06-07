#!/usr/bin/env python3
"""
🎯 Агент приоритизации — третий режим бота @AuditorWelcs_bot.

Поток:
  1. build_draft(question)  — Claude читает цели и текущую загрузку в Notion
     (read-only), проверяет дубли, рекомендует приоритет/исполнителя и через
     инструмент propose_entry возвращает ЧЕРНОВИК записи. Ничего не пишет.
  2. бот показывает черновик с кнопками [✅ Записать][✏️ Править][❌ Отмена].
  3. commit_draft(draft) — по ✅ создаёт запись в Notion Inbox и, если задача
     делегирована, ставит задачу руководителю в Bitrix24.

Цель этого инструмента = реализация OKR O3 «единый канал постановки задач».
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
BITRIX_DIR = HERE.parent / "bitrix-agent"
sys.path.insert(0, str(BITRIX_DIR))

# страница «WEEK GOALS» (текущие цели на 1–2 недели)
WEEK_GOALS_PAGE = "b3a8585e-a32f-8272-b2d2-0142e1303050"

# Ответственный (Notion) → Bitrix user ID. Михаил=сам, остальные — руководители.
MANAGERS = {
    "Михаил (сам)": 1,
    "J. Bada": 6886,      # Director Comercial
    "Diana": 13016,       # Responsable de zona Baix Empordà
    "Takhmina": 31290,    # tech@welcs.com
    "Ksenia": 29538,      # HR manager
    "Coen": 18702,        # Departamento Revenue
    "Corina": 32428,      # Operations Specialist
    "Sofia": 32116,       # Senior Customer Support
    "Dev": 30386,         # Developer
}

OKR = """\
O1 Платформа — роадмап продукта, процесс разработки, единый backlog, экосистема.
O2 AI-агенты — сценарии агентов, база знаний, пилот, метрики.
O3 Задачи и автоматизации — единый канал постановки задач, SLA, автоматизации (← этот бот реализует именно эту цель).
O4 Xero — выбор подрядчика, миграция учёта в Xero, обучение.
O5 Личный рост — обучение по архитектуре/AI, курсы, микропроекты, карта компетенций."""

SYSTEM = f"""\
Ты — личный ассистент по приоритизации руководителя компании Welcs (управление \
арендой недвижимости, Costa Brava). Он сбрасывает тебе в Telegram мысли, идеи, \
тревоги и задачи. Твоя работа — превратить это в чёткую запись, привязанную к его \
целям, с правильным приоритетом и, если уместно, делегированием.

ЕГО ЦЕЛИ (OKR на квартал):
{OKR}

АЛГОРИТМ:
1. Пойми ТИП ввода: Задача / Идея / Цель / Мысль/тревога / Вопрос.
2. Прочитай текущие цели недели: вызови read_week_goals.
3. Посмотри текущую загрузку и проверь дубли: вызови get_inbox (открытые записи).
   Если уже есть похожая запись — отметь это в duplicate_of и не плоди дубль.
4. Привяжи к одной из целей O1–O5 (или «Без цели», если не относится).
5. Оцени приоритет: High только если двигает ключевую цель/есть срок; иначе Medium/Low.
6. Реши, делать самому или ДЕЛЕГИРОВАТЬ. Если задача операционная или вне зоны \
   руководителя — предложи ответственного из списка и поставь delegate_to_bitrix=true.
   Доступные руководители: {", ".join(k for k in MANAGERS if k != "Михаил (сам)")}.
7. Учитывай загрузку: если на эту неделю уже много High-задач у Михаила — предложи \
   следующую неделю или делегирование, и скажи об этом в reasoning.
8. Вызови propose_entry с финальным черновиком. reasoning — кратко по-русски: почему \
   такой приоритет, такая цель, такой исполнитель, есть ли перегруз/дубль.

Будь конкретным и кратким. Не выдумывай дедлайны — ставь только если они следуют из \
текста. Тревоги без действия — это Тип «Мысль/тревога», часто «Без цели», Low."""

TOOLS = [
    {"name": "read_week_goals",
     "description": "Прочитать текущие цели недели (страница WEEK GOALS в Notion).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_inbox",
     "description": "Список текущих записей в базе Инбокса (по умолчанию только не завершённые) "
                    "— для проверки дублей и оценки загрузки.",
     "input_schema": {"type": "object", "properties": {
         "open_only": {"type": "boolean", "description": "Только не-Done (по умолчанию true)."}}}},
    {"name": "propose_entry",
     "description": "Финальный черновик записи. Вызывай ОДИН раз в конце.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string", "description": "Короткое чёткое название задачи/идеи."},
         "tip": {"type": "string", "enum": ["Задача", "Идея", "Цель", "Мысль/тревога", "Вопрос"]},
         "objective": {"type": "string",
                       "enum": ["O1 Платформа", "O2 AI-агенты", "O3 Задачи и автоматизации",
                                "O4 Xero", "O5 Личный рост", "Без цели"]},
         "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
         "responsible": {"type": "string", "enum": list(MANAGERS.keys())},
         "week": {"type": "string", "description": "Напр. 'эта неделя', 'след. неделя', '3–4'. Можно пусто."},
         "deadline": {"type": "string", "description": "YYYY-MM-DD, только если есть явный срок."},
         "done_when": {"type": "string", "description": "Критерий готовности (что считается готово)."},
         "delegate_to_bitrix": {"type": "boolean",
                                "description": "Поставить ли реальную задачу руководителю в Bitrix24."},
         "duplicate_of": {"type": "string", "description": "Название/URL похожей записи, если нашёл дубль."},
         "reasoning": {"type": "string", "description": "Краткое обоснование по-русски (1–3 предложения)."}},
         "required": ["title", "tip", "objective", "priority", "responsible", "reasoning"]}},
]


def _notion(env):
    from notion_client import Notion
    return Notion(env["NOTION_API_KEY"])


def _tool_read_goals(env) -> str:
    try:
        txt = _notion(env).get_page_text(WEEK_GOALS_PAGE)
        return txt or "(страница целей пуста)"
    except Exception as e:  # noqa: BLE001
        return f"(не удалось прочитать цели: {e})"


def _tool_get_inbox(env, open_only: bool) -> str:
    try:
        filt = None
        if open_only:
            filt = {"property": "Статус", "select": {"does_not_equal": "Done"}}
        rows = _notion(env).query_inbox(filter_=filt, page_size=80)
        slim = [{"Название": r.get("Название"), "Тип": r.get("Тип"),
                 "Objective": r.get("Objective"), "Приоритет": r.get("Приоритет"),
                 "Ответственный": r.get("Ответственный"), "Неделя": r.get("Неделя"),
                 "Статус": r.get("Статус")} for r in rows]
        return json.dumps({"count": len(slim), "items": slim}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def build_draft(question: str, env: dict) -> dict:
    """Прогоняет ввод через Claude, возвращает {draft: {...}} или {error / text}."""
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        resp = client.messages.create(
            model=model, max_tokens=2000,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"text": text or "Не понял мысль — переформулируй, пожалуйста."}
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if b.name == "propose_entry":
                return {"draft": dict(b.input)}
            if b.name == "read_week_goals":
                out = _tool_read_goals(env)
            elif b.name == "get_inbox":
                out = _tool_get_inbox(env, b.input.get("open_only", True))
            else:
                out = json.dumps({"error": f"неизвестный инструмент {b.name}"}, ensure_ascii=False)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
    return {"text": "Слишком долго думал — попробуй сформулировать короче."}


def format_draft(d: dict) -> str:
    """Человекочитаемый черновик для Telegram."""
    lines = [f"<b>{d.get('title', '—')}</b>",
             f"Тип: {d.get('tip', '—')}  ·  Цель: {d.get('objective', '—')}",
             f"Приоритет: {d.get('priority', '—')}  ·  Ответственный: {d.get('responsible', '—')}"]
    if d.get("week"):
        lines.append(f"Неделя: {d['week']}")
    if d.get("deadline"):
        lines.append(f"Дедлайн: {d['deadline']}")
    if d.get("done_when"):
        lines.append(f"Готово, когда: {d['done_when']}")
    if d.get("delegate_to_bitrix"):
        lines.append(f"➡️ Поставлю задачу в Bitrix: <b>{d.get('responsible')}</b>")
    if d.get("duplicate_of"):
        lines.append(f"⚠️ Возможный дубль: {d['duplicate_of']}")
    lines.append(f"\n<i>{d.get('reasoning', '')}</i>")
    return "\n".join(lines)


def commit_draft(d: dict, env: dict) -> str:
    """Создаёт запись в Notion и (если делегировано) задачу в Bitrix. Возвращает отчёт."""
    report = []

    # 1) Bitrix-задача (если делегировано не на самого себя)
    bitrix_ref = None
    resp_name = d.get("responsible")
    if d.get("delegate_to_bitrix") and resp_name and resp_name != "Михаил (сам)":
        uid = MANAGERS.get(resp_name)
        if not uid:
            report.append(f"⚠️ Не нашёл Bitrix ID для {resp_name} — задачу в Bitrix не поставил.")
        elif not env.get("BITRIX_WEBHOOK_URL"):
            report.append("⚠️ Нет BITRIX_WEBHOOK_URL — задачу в Bitrix не поставил.")
        else:
            try:
                from bitrix_client import Bitrix
                bx = Bitrix(env["BITRIX_WEBHOOK_URL"])
                fields = {
                    "TITLE": d.get("title", "Задача"),
                    "RESPONSIBLE_ID": uid,
                    "CREATED_BY": MANAGERS["Михаил (сам)"],
                    "DESCRIPTION": (d.get("done_when") or "") +
                                   (f"\n\nЦель: {d.get('objective')}" if d.get("objective") else "") +
                                   "\n\n(Поставлено через бот приоритизации @AuditorWelcs_bot)",
                }
                if d.get("deadline"):
                    fields["DEADLINE"] = d["deadline"]
                res = bx.call("tasks.task.add", {"fields": fields})
                tid = (res or {}).get("task", {}).get("id") if isinstance(res, dict) else None
                if tid:
                    bitrix_ref = f"Bitrix #{tid} → {resp_name}"
                    report.append(f"✅ Задача в Bitrix поставлена: #{tid} → {resp_name}")
                else:
                    report.append(f"✅ Задача в Bitrix отправлена ({resp_name}).")
            except Exception as e:  # noqa: BLE001
                report.append(f"⚠️ Bitrix: не удалось поставить задачу — {e}")

    # 2) Запись в Notion Inbox
    try:
        week = d.get("week")
        deadline = d.get("deadline") or None
        n = _notion(env)
        res = n.create_inbox_entry(
            title=d.get("title", "Без названия"),
            tip=d.get("tip"), objective=d.get("objective"),
            priority=d.get("priority"), responsible=resp_name,
            week=week, deadline=deadline, done_when=d.get("done_when"),
            bitrix_task=bitrix_ref)
        report.append(f"✅ Записал в Notion: <a href=\"{res['url']}\">{d.get('title')}</a>")
    except Exception as e:  # noqa: BLE001
        report.append(f"⚠️ Notion: не удалось записать — {e}")

    return "\n".join(report)


def today() -> str:
    return dt.date.today().isoformat()
