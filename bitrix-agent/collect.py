"""
Сбор данных из Bitrix24 за последние N часов:
  - открытые линии (диалоги с клиентами)
  - внутренние чаты команды
  - сделки и лиды CRM (с комментариями таймлайна)
  - задачи и сроки (просрочки, без ответа)

Возвращает один словарь, который потом уходит в анализ Claude.
Всё ограничено лимитами, чтобы не раздувать контекст и не упереться в API.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from bitrix_client import Bitrix


def _iso(hours_back: int) -> str:
    since = dt.datetime.now() - dt.timedelta(hours=hours_back)
    return since.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_dt(s: str) -> dt.datetime | None:
    """Парсим дату Bitrix (ISO с офсетом) в timezone-aware datetime."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    # без офсета — считаем локальной зоной
    return d if d.tzinfo else d.astimezone()


def now_aware() -> dt.datetime:
    return dt.datetime.now().astimezone()


# системные строки, которые не несут смысла для анализа
_SYSTEM_MARKERS = (
    "joined the team", "joined the group", "I have joined", "Me uní",
    "se unió al grupo", "вступил", "sent invitation", "отправил приглашение",
    "registered", "registr", "добавил права", "Use this chat to ask",
)


def _is_system(text: str) -> bool:
    return any(m.lower() in text.lower() for m in _SYSTEM_MARKERS)


def build_user_map(bx: Bitrix, errors: list[str]) -> dict[str, str]:
    """ID сотрудника -> 'Имя Фамилия (должность)'. При нехватке прав — пустая карта."""
    out: dict[str, str] = {}
    try:
        users = bx.call_list("user.get", {"FILTER": {"ACTIVE": True}}, max_items=1000)
    except Exception as e:  # noqa: BLE001
        errors.append(f"user.get ({e}) — имена сотрудников не подтянутся, нужен scope 'user'")
        return out
    for u in users:
        name = " ".join(filter(None, [u.get("NAME"), u.get("LAST_NAME")])).strip()
        name = name or u.get("EMAIL") or f"user{u.get('ID')}"
        pos = u.get("WORK_POSITION")
        out[str(u.get("ID"))] = f"{name} ({pos})" if pos else name
    return out


def collect_dialogs(bx: Bitrix, users: dict[str, str], hours: int,
                    max_dialogs: int, msgs_per_dialog: int,
                    errors: list[str]) -> dict[str, list]:
    """
    Открытые линии (клиенты) + внутренние чаты команды.
    im.recent.list возвращает недавние диалоги; тип 'lines' = открытая линия,
    'chat' = групповой чат, 'user' = личка.
    """
    open_lines: list[dict] = []
    team_chats: list[dict] = []
    try:
        recent = bx.call("im.recent.list", {"SKIP_OPENLINES": "N"}) or {}
    except Exception as e:  # noqa: BLE001
        errors.append(f"im.recent.list ({e}) — диалоги не собраны")
        return {"open_lines": [], "team_chats": []}

    items = recent.get("items", recent) if isinstance(recent, dict) else recent
    if not isinstance(items, list):
        items = []

    for item in items[: max_dialogs * 3]:
        dialog_id = item.get("id") or item.get("chat_id")
        if not dialog_id:
            continue
        kind = (item.get("type") or "").lower()
        # признак открытой линии — внутри chat.entity_type == 'LINES',
        # а верхний type обманчиво равен 'chat'
        entity_type = ((item.get("chat") or {}).get("entity_type") or "").upper()
        is_openline = entity_type == "LINES" or "lines" in kind
        title = item.get("title") or (item.get("user") or {}).get("name") or str(dialog_id)
        try:
            raw = bx.call("im.dialog.messages.get",
                          {"DIALOG_ID": dialog_id, "LIMIT": msgs_per_dialog}) or {}
        except Exception:  # noqa: BLE001
            continue
        msgs = raw.get("messages", [])
        if not msgs:
            continue
        floor = now_aware() - dt.timedelta(hours=hours)
        thread = []
        for m in msgs:
            text = (m.get("text") or "").strip()
            mdt = _parse_dt(m.get("date") or "")
            # только свежие сообщения в окне и только осмысленные (с текстом, не система)
            if mdt and mdt < floor:
                continue
            if not text or _is_system(text):
                continue
            author = users.get(str(m.get("author_id")), f"id{m.get('author_id')}")
            thread.append({"date": m.get("date"), "author": author, "text": text})
        if not thread:  # пустой/шумовой диалог пропускаем
            continue
        record = {"dialog_id": dialog_id, "title": title, "messages": thread}
        if is_openline:
            if len(open_lines) < max_dialogs:
                open_lines.append(record)
        elif kind in ("chat", "user"):
            if len(team_chats) < max_dialogs:
                team_chats.append(record)

    return {"open_lines": open_lines, "team_chats": team_chats}


def collect_crm(bx: Bitrix, users: dict[str, str], hours: int, max_items: int,
                errors: list[str]) -> dict[str, list]:
    since = _iso(hours)
    out: dict[str, list] = {"deals": [], "leads": []}

    def _owner(uid: Any) -> str:
        return users.get(str(uid), f"id{uid}")

    try:
        deals = bx.call_list("crm.deal.list", {
            "FILTER": {">=DATE_MODIFY": since},
            "SELECT": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "ASSIGNED_BY_ID", "DATE_MODIFY"],
            "ORDER": {"DATE_MODIFY": "DESC"},
        }, max_items=max_items)
        for d in deals:
            out["deals"].append({
                "id": d.get("ID"),
                "title": d.get("TITLE"),
                "stage": d.get("STAGE_ID"),
                "amount": d.get("OPPORTUNITY"),
                "owner": _owner(d.get("ASSIGNED_BY_ID")),
            })
    except Exception as e:  # noqa: BLE001
        errors.append(f"crm.deal.list ({e})")

    try:
        leads = bx.call_list("crm.lead.list", {
            "FILTER": {">=DATE_MODIFY": since},
            "SELECT": ["ID", "TITLE", "STATUS_ID", "ASSIGNED_BY_ID", "DATE_MODIFY"],
            "ORDER": {"DATE_MODIFY": "DESC"},
        }, max_items=max_items)
        for l in leads:
            out["leads"].append({
                "id": l.get("ID"),
                "title": l.get("TITLE"),
                "status": l.get("STATUS_ID"),
                "owner": _owner(l.get("ASSIGNED_BY_ID")),
            })
    except Exception as e:  # noqa: BLE001
        errors.append(f"crm.lead.list ({e})")
    return out


def _timeline(bx: Bitrix, entity: str, entity_id: Any, limit: int = 5) -> list[str]:
    try:
        comments = bx.call_list("crm.timeline.comment.list", {
            "filter": {"ENTITY_ID": entity_id, "ENTITY_TYPE": entity},
            "order": {"CREATED": "DESC"},
        }, max_items=limit)
    except Exception:  # noqa: BLE001
        return []
    return [(c.get("COMMENT") or "").strip() for c in comments if c.get("COMMENT")]


def collect_tasks(bx: Bitrix, users: dict[str, str], max_items: int,
                  errors: list[str]) -> list[dict]:
    """Просроченные / зависшие задачи."""
    now_dt = dt.datetime.now()
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    # «свежие» просрочки: дедлайн в пределах последних N дней (по умолч. 30),
    # чтобы не утонуть в многолетнем бэклоге. Самые недавние — сверху.
    floor = (now_dt - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        tasks = bx.call_list("tasks.task.list", {
            "filter": {"<DEADLINE": now, ">DEADLINE": floor, "!STATUS": "5"},
            "select": ["ID", "TITLE", "DEADLINE", "RESPONSIBLE_ID", "STATUS", "CREATED_DATE"],
            "order": {"DEADLINE": "desc"},
        }, max_items=max_items)
    except Exception as e:  # noqa: BLE001
        errors.append(f"tasks.task.list ({e})")
        return []
    out = []
    for t in tasks:
        uid = t.get("responsibleId") or t.get("RESPONSIBLE_ID")
        out.append({
            "id": t.get("id") or t.get("ID"),
            "title": t.get("title") or t.get("TITLE"),
            "deadline": t.get("deadline") or t.get("DEADLINE"),
            "responsible": users.get(str(uid), f"id{uid}"),
        })
    return out


def _apt_number(title: str) -> str | None:
    """Достаём номер апартамента из заголовка задачи/диалога (нормализуем без ведущих нулей)."""
    m = re.search(r"WELCS\s*0*(\d{2,4})", title, re.I)
    if m:
        return str(int(m.group(1)))
    # '(NNN)' в названии — обычно номер объекта
    nums = re.findall(r"\((?:ex\s*\d+\s*)?0*(\d{2,4})", title)
    if nums:
        return str(int(nums[-1]))
    # инциденты без скобок: 'Wifi 339', 'Reclamo Truvi 287', 'Desagüe aire 012'
    trail = re.findall(r"\b0*(\d{2,3})\b", title)
    return str(int(trail[-1])) if trail else None


def _apt_category(title: str) -> str:
    t = title.lower()
    if re.search(r"reclam|queja|truvi", t):
        return "reclamo"
    if re.search(r"check.?in|entrada|presencial|llave|salida|retorno", t):
        return "checkin"
    if re.search(r"wifi|desag|aire|aver|repar|caldera|fuga|agua|luz|electric|roto|no func|incid", t):
        return "incidencia"
    if re.match(r"\s*limpieza", t):
        return "limpieza"
    return "otro"


def collect_apartment_ops(bx: Bitrix, errors: list[str], days: int = 7,
                          max_items: int = 300) -> list[dict]:
    """Задачи по апартаментам за последние N дней: уборки, заезды, рекламации, инциденты."""
    since = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        tasks = bx.call_list("tasks.task.list", {
            "filter": {">CHANGED_DATE": since},
            "select": ["ID", "TITLE", "STATUS", "CHANGED_DATE", "RESPONSIBLE_ID"],
            "order": {"CHANGED_DATE": "desc"},
        }, max_items=max_items)
    except Exception as e:  # noqa: BLE001
        errors.append(f"apartment_ops/tasks.task.list ({e})")
        return []
    out = []
    for t in tasks:
        title = t.get("title") or t.get("TITLE") or ""
        cat = _apt_category(title)
        if cat == "otro":
            continue
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", title)
        date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
        out.append({
            "id": t.get("id") or t.get("ID"),
            "title": title,
            "category": cat,
            "apartment": _apt_number(title),
            "date": date,
            "status": str(t.get("status") or t.get("STATUS")),
        })
    return out


def collect_all(bx: Bitrix, cfg: dict) -> dict[str, Any]:
    hours = cfg.get("LOOKBACK_HOURS", 24)
    errors: list[str] = []
    users = build_user_map(bx, errors)
    dialogs = collect_dialogs(
        bx, users, hours,
        max_dialogs=cfg.get("MAX_DIALOGS", 40),
        msgs_per_dialog=cfg.get("MSGS_PER_DIALOG", 30),
        errors=errors,
    )
    crm = collect_crm(bx, users, hours, max_items=cfg.get("MAX_CRM", 60), errors=errors)
    tasks = collect_tasks(bx, users, max_items=cfg.get("MAX_TASKS", 50), errors=errors)
    apartment_ops = collect_apartment_ops(bx, errors)
    return {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lookback_hours": hours,
        "open_lines": dialogs.get("open_lines", []),
        "team_chats": dialogs.get("team_chats", []),
        "deals": crm["deals"],
        "leads": crm["leads"],
        "overdue_tasks": tasks,
        "apartment_ops": apartment_ops,
        "_collect_errors": errors or None,
    }
