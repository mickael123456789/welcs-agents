"""
Сбор и подготовка встреч за период.

Делаем три вещи:
  1. Тянем все встречи в окне.
  2. Классифицируем каждую: внутренняя (команда), собеседование, внешняя
     (партнёр/инвестор/клиент) — по доменам участников и эвристикам названия.
  3. Помечаем встречи, где участвовал сам руководитель (его обязательства/дедлайны).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

# Домены и адреса, считающиеся «своей командой».
DEFAULT_TEAM_DOMAINS = {"welcs.com", "welcs.app"}
# Доп. личные адреса сотрудников (gmail и т.п.) — дополняется из .env.
DEFAULT_TEAM_EMAILS = {"yuliabalueva.welcs@gmail.com"}

# Слова в названии встречи, указывающие на собеседование с кандидатом.
INTERVIEW_HINTS = re.compile(
    r"\b(собес|интервью|interview|кандидат|candidat|recruit|hr\s|вакансия|hiring|"
    r"meet\s*&\s*greet|meet\s*and\s*greet)\b",
    re.IGNORECASE,
)


def _emails_of(t: dict[str, Any]) -> set[str]:
    emails: set[str] = set()
    for e in (t.get("participants") or []):
        if e:
            emails.add(e.lower())
    for a in (t.get("meeting_attendees") or []):
        e = (a or {}).get("email")
        if e:
            emails.add(e.lower())
    for k in ("organizer_email", "host_email"):
        if t.get(k):
            emails.add(t[k].lower())
    return emails


def _domain(email: str) -> str:
    return email.split("@", 1)[1] if "@" in email else ""


def classify(
    t: dict[str, Any],
    me_emails: set[str],
    team_domains: set[str],
    team_emails: set[str],
) -> dict[str, Any]:
    emails = _emails_of(t)
    internal = {e for e in emails if _domain(e) in team_domains or e in team_emails}
    external = emails - internal

    title = t.get("title") or ""
    is_interview = bool(INTERVIEW_HINTS.search(title)) or (
        (t.get("organizer_email") or "").lower().startswith("hr@") and bool(external)
    )

    if is_interview:
        kind = "interview"
    elif external:
        kind = "external"  # партнёр / инвестор / клиент
    else:
        kind = "internal"  # внутренняя встреча команды

    me_present = bool(emails & me_emails)

    return {
        "kind": kind,
        "me_present": me_present,
        "internal_emails": sorted(internal),
        "external_emails": sorted(external),
    }


def _summary_block(t: dict[str, Any]) -> dict[str, Any]:
    s = t.get("summary") or {}
    return {
        "short_summary": s.get("short_summary") or s.get("overview"),
        "keywords": s.get("keywords"),
        "action_items": s.get("action_items"),
        "meeting_type": s.get("meeting_type"),
    }


def collect_all(
    client,
    cfg: dict[str, Any],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    lookback = cfg["LOOKBACK_HOURS"]
    from_dt = now - dt.timedelta(hours=lookback)
    from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    me_emails = set(cfg["ME_EMAILS"])
    team_domains = set(cfg["TEAM_DOMAINS"])
    team_emails = set(cfg["TEAM_EMAILS"])

    errors: list[str] = []
    try:
        raw = client.list_transcripts(from_iso, to_iso, limit=cfg.get("MAX_MEETINGS", 50))
    except Exception as e:  # noqa: BLE001
        raw = []
        errors.append(f"Не удалось получить встречи: {e}")

    meetings: list[dict[str, Any]] = []
    for t in raw:
        meta = classify(t, me_emails, team_domains, team_emails)
        meetings.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "date": t.get("dateString"),
            "duration_min": round(t.get("duration") or 0, 1),
            "organizer": t.get("organizer_email"),
            "attendees": [
                (a or {}).get("displayName") or (a or {}).get("name") or (a or {}).get("email")
                for a in (t.get("meeting_attendees") or [])
            ],
            **meta,
            "summary": _summary_block(t),
        })

    # сортируем по дате (новые внизу — так читается как лента дня)
    meetings.sort(key=lambda m: m.get("date") or "")

    counts = {
        "internal": sum(1 for m in meetings if m["kind"] == "internal"),
        "interview": sum(1 for m in meetings if m["kind"] == "interview"),
        "external": sum(1 for m in meetings if m["kind"] == "external"),
        "my_meetings": sum(1 for m in meetings if m["me_present"]),
    }

    return {
        "generated_at": now.isoformat(),
        "lookback_hours": lookback,
        "window": {"from": from_iso, "to": to_iso},
        "counts": counts,
        "meetings": meetings,
        "_collect_errors": errors,
    }
