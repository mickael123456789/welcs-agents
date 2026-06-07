"""
Детерминированная аналитика поверх собранных данных — считается в коде,
чтобы цифры были точными, а Claude уже их интерпретировал.

Считаем:
  1. SLA открытых линий — кто ждёт ответа и сколько; что без ответа.
  2. Карточку операторов — нагрузка и скорость ответа.
  3. Готовность к заездам — уборки на сегодня/завтра, которые ещё не сделаны.
  4. Связку «апартамент ↔ чат ↔ задача» — инциденты в одном месте.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any

from collect import _apt_number, _parse_dt, now_aware

# статусы задач Bitrix
DONE_STATUSES = {"4", "5"}  # 4 = ждёт контроля (готова), 5 = завершена


def _role(msg: dict) -> str:
    """client / operator / system по автору и тексту сообщения открытой линии."""
    author = msg.get("author", "")
    text = msg.get("text", "")
    if text.startswith("=== Outgoing message"):
        return "operator"
    if author == "id0":
        return "system"
    if re.match(r"^id\d+$", author):
        return "client"
    return "operator"  # автор с настоящим именем = сотрудник


def _op_name(msg: dict) -> str:
    text = msg.get("text", "")
    m = re.search(r"author:\s*Bitrix24\s*\(([^)]+)\)", text)
    if m:
        return m.group(1).strip()
    return re.sub(r"\s*\(.*\)$", "", msg.get("author", "")).strip()


def open_line_sla(data: dict, now: dt.datetime) -> dict[str, Any]:
    """Для каждого клиентского диалога: ответили ли и сколько ждали/ждут."""
    waiting, answered = [], []
    for d in data.get("open_lines", []):
        msgs = [m for m in d.get("messages", []) if _role(m) != "system"]
        if not msgs:
            continue
        msgs.sort(key=lambda m: m.get("date") or "")
        # последнее сообщение клиента
        last_client_i = None
        for i in range(len(msgs) - 1, -1, -1):
            if _role(msgs[i]) == "client":
                last_client_i = i
                break
        if last_client_i is None:
            continue
        cmsg = msgs[last_client_i]
        cdt = _parse_dt(cmsg.get("date") or "")
        # есть ли ответ оператора ПОСЛЕ него
        op_after = next((m for m in msgs[last_client_i + 1:] if _role(m) == "operator"), None)
        apt = _apt_number(d.get("title", ""))
        rec = {
            "dialog": d.get("title"),
            "dialog_id": d.get("dialog_id"),
            "apartment": apt,
            "last_client_text": cmsg.get("text", "")[:200],
            "last_client_time": cmsg.get("date"),
        }
        if op_after:
            odt = _parse_dt(op_after.get("date") or "")
            mins = round((odt - cdt).total_seconds() / 60) if (odt and cdt) else None
            rec.update({"answered": True, "response_min": mins, "operator": _op_name(op_after)})
            answered.append(rec)
        else:
            mins = round((now - cdt).total_seconds() / 60) if cdt else None
            rec.update({"answered": False, "waiting_min": mins})
            waiting.append(rec)
    waiting.sort(key=lambda r: r.get("waiting_min") or 0, reverse=True)
    return {
        "unanswered": waiting,
        "answered": answered,
        "unanswered_count": len(waiting),
        "max_wait_min": waiting[0]["waiting_min"] if waiting else 0,
    }


def operator_scorecard(data: dict) -> list[dict]:
    """Нагрузка и средняя скорость ответа по операторам открытых линий."""
    replies: dict[str, int] = defaultdict(int)
    dialogs: dict[str, set] = defaultdict(set)
    resp_times: dict[str, list] = defaultdict(list)
    for d in data.get("open_lines", []):
        msgs = [m for m in d.get("messages", []) if _role(m) != "system"]
        msgs.sort(key=lambda m: m.get("date") or "")
        prev_client_dt = None
        for m in msgs:
            r = _role(m)
            if r == "client":
                prev_client_dt = _parse_dt(m.get("date") or "")
            elif r == "operator":
                name = _op_name(m)
                replies[name] += 1
                dialogs[name].add(d.get("title"))
                odt = _parse_dt(m.get("date") or "")
                if prev_client_dt and odt:
                    resp_times[name].append((odt - prev_client_dt).total_seconds() / 60)
                    prev_client_dt = None
    out = []
    for name in replies:
        rts = resp_times[name]
        out.append({
            "operator": name,
            "replies": replies[name],
            "dialogs": len(dialogs[name]),
            "median_response_min": round(sorted(rts)[len(rts) // 2]) if rts else None,
        })
    out.sort(key=lambda r: r["replies"], reverse=True)
    return out


def checkin_readiness(data: dict, now: dt.datetime) -> dict[str, list]:
    """Уборки на сегодня/завтра, которые ещё не сделаны (риск к заезду)."""
    today = now.date().isoformat()
    tomorrow = (now.date() + dt.timedelta(days=1)).isoformat()
    res = {"today": [], "tomorrow": []}
    for t in data.get("apartment_ops", []):
        if t["category"] != "limpieza" or not t.get("date"):
            continue
        if t["status"] in DONE_STATUSES:
            continue
        if t["date"] == today:
            res["today"].append({"apartment": t["apartment"], "title": t["title"], "id": t["id"]})
        elif t["date"] == tomorrow:
            res["tomorrow"].append({"apartment": t["apartment"], "title": t["title"], "id": t["id"]})
    return res


def incidents(data: dict) -> list[dict]:
    """Открытые рекламации и инциденты по апартаментам (не уборка)."""
    out = []
    for t in data.get("apartment_ops", []):
        if t["category"] in ("reclamo", "incidencia") and t["status"] not in DONE_STATUSES:
            out.append({
                "apartment": t["apartment"], "type": t["category"],
                "title": t["title"], "id": t["id"], "status": t["status"],
            })
    return out


def link_apartments(data: dict, sla: dict, inc: list, readiness: dict) -> list[dict]:
    """Сшиваем апартамент по сигналам: чат клиента + инцидент + несделанная уборка.
    Кейс считается «составным», если апартамент встречается минимум в двух источниках."""
    by_apt: dict[str, dict] = defaultdict(lambda: {"chat": [], "incidents": [], "cleaning_pending": []})
    for w in sla["unanswered"] + sla["answered"]:
        if w.get("apartment"):
            by_apt[w["apartment"]]["chat"].append({
                "answered": w.get("answered"), "text": w.get("last_client_text"),
            })
    for i in inc:
        if i.get("apartment"):
            by_apt[i["apartment"]]["incidents"].append(i)
    for when in ("today", "tomorrow"):
        for r in readiness.get(when, []):
            if r.get("apartment"):
                by_apt[r["apartment"]]["cleaning_pending"].append({**r, "when": when})
    linked = []
    for apt, v in by_apt.items():
        signals = sum(1 for k in ("chat", "incidents", "cleaning_pending") if v[k])
        if signals >= 2:  # минимум два разных источника → составной кейс
            linked.append({"apartment": apt, **v})
    return linked


def compute(data: dict, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or now_aware()
    sla = open_line_sla(data, now)
    inc = incidents(data)
    readiness = checkin_readiness(data, now)
    return {
        "sla": sla,
        "operators": operator_scorecard(data),
        "readiness": readiness,
        "incidents": inc,
        "linked_cases": link_apartments(data, sla, inc, readiness),
    }
