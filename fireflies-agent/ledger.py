"""
Реестр обязательств и дедлайнов.

Из встреч руководителя извлекаются договорённости вида «X должен сделать Y к дате Z».
Они копятся в commitments.json, чтобы в день дедлайна о них можно было напомнить —
даже если встреча, где это пообещали, была неделю назад.

Каждое обязательство:
  id          — стабильный хэш (для дедупликации между запусками)
  who         — кто отвечает
  what        — что должен сделать
  due_date    — дедлайн (YYYY-MM-DD) или null, если срок не назван
  to_me       — должен ли отчитаться/сделать это ДЛЯ руководителя
  source_id   — id встречи-источника
  source_title, source_date
  status      — open | done   (done выставляется, если в новой встрече видно, что закрыто)
  first_seen, last_seen
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def make_id(who: str, what: str, source_id: str) -> str:
    h = hashlib.sha1(f"{_norm(who)}|{_norm(what)}|{source_id}".encode()).hexdigest()
    return h[:12]


def load(path: Path) -> list[dict[str, Any]]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
    return []


def save(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def merge(existing: list[dict[str, Any]], extracted: list[dict[str, Any]], today: str) -> list[dict[str, Any]]:
    """Добавляем новые обязательства, обновляем last_seen у известных."""
    by_id = {c["id"]: c for c in existing}
    for c in extracted:
        cid = c["id"]
        if cid in by_id:
            by_id[cid]["last_seen"] = today
            # подтянуть дедлайн, если раньше был неизвестен
            if not by_id[cid].get("due_date") and c.get("due_date"):
                by_id[cid]["due_date"] = c["due_date"]
        else:
            c.setdefault("status", "open")
            c["first_seen"] = today
            c["last_seen"] = today
            by_id[cid] = c
    return list(by_id.values())


def due_buckets(items: list[dict[str, Any]], today: str, horizon_days: int = 3) -> dict[str, list]:
    """Разбиваем открытые обязательства на: просрочено / сегодня / ближайшие N дней."""
    today_d = dt.date.fromisoformat(today)
    overdue, due_today, upcoming = [], [], []
    for c in items:
        if c.get("status") == "done":
            continue
        dd = c.get("due_date")
        if not dd:
            continue
        try:
            d = dt.date.fromisoformat(dd)
        except ValueError:
            continue
        delta = (d - today_d).days
        if delta < 0:
            overdue.append(c)
        elif delta == 0:
            due_today.append(c)
        elif delta <= horizon_days:
            upcoming.append(c)
    key = lambda c: c.get("due_date") or ""
    return {
        "overdue": sorted(overdue, key=key),
        "due_today": sorted(due_today, key=key),
        "upcoming": sorted(upcoming, key=key),
    }
