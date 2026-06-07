"""
Тонкая обёртка над Notion REST API (через internal integration token).

Нужна для агента приоритизации: читать цели/текущую загрузку и писать новые
записи в базу «📥 Инбокс идей и задач».

Токен: создать на https://www.notion.so/my-integrations → Internal Integration
Secret (NOTION_API_KEY). Базу нужно расшарить интеграции через •••→Connections.

Документация: https://developers.notion.com/reference
"""

from __future__ import annotations

import time
from typing import Any

import requests

NOTION_VERSION = "2022-06-28"
BASE = "https://api.notion.com/v1"

# database_id новой базы «📥 Инбокс идей и задач»
INBOX_DB_ID = "79e692c5-9517-497b-835a-4b637da8ae2e"


class NotionError(RuntimeError):
    pass


class Notion:
    def __init__(self, token: str, timeout: int = 30):
        if not token:
            raise NotionError("Не задан NOTION_API_KEY")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = BASE + path
        for attempt in range(5):
            resp = self.session.request(method, url, json=payload, timeout=self.timeout)
            if resp.status_code == 429:  # rate limited
                time.sleep(float(resp.headers.get("Retry-After", 1.5)) + attempt)
                continue
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                raise NotionError(f"{method} {path}: {data.get('code')} — {data.get('message')}")
            return data
        raise NotionError(f"{method} {path}: превышено число попыток (429)")

    # ── чтение ────────────────────────────────────────────────────────────────

    def query_inbox(self, filter_: dict | None = None, sorts: list | None = None,
                    page_size: int = 50) -> list[dict]:
        """Запрос к базе Инбокса. Возвращает список упрощённых записей."""
        payload: dict[str, Any] = {"page_size": min(page_size, 100)}
        if filter_:
            payload["filter"] = filter_
        if sorts:
            payload["sorts"] = sorts
        data = self._request("POST", f"/databases/{INBOX_DB_ID}/query", payload)
        return [_simplify_page(p) for p in data.get("results", [])]

    def get_page_text(self, page_id: str, max_blocks: int = 200) -> str:
        """Плоский текст страницы Notion (для чтения целей/WEEK GOALS)."""
        out: list[str] = []
        cursor = None
        for _ in range(10):
            path = f"/blocks/{page_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self._request("GET", path)
            for b in data.get("results", []):
                txt = _block_text(b)
                if txt:
                    out.append(txt)
                if len(out) >= max_blocks:
                    return "\n".join(out)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(out)

    # ── запись ──────────────────────────────────────────────────────────────────

    def create_inbox_entry(self, *, title: str, tip: str | None = None,
                           objective: str | None = None, status: str = "Not started",
                           priority: str | None = None, responsible: str | None = None,
                           week: str | None = None, deadline: str | None = None,
                           done_when: str | None = None, expected_result: str | None = None,
                           reasoning: str | None = None, source: str = "Telegram-бот",
                           bitrix_task: str | None = None) -> dict:
        """Создаёт запись в базе Инбокса. Возвращает {id, url}."""
        props: dict[str, Any] = {
            "Название": {"title": [{"text": {"content": title[:2000]}}]},
            "Статус": {"select": {"name": status}},
            "Источник": {"select": {"name": source}},
        }
        if tip:
            props["Тип"] = {"select": {"name": tip}}
        if objective:
            props["Objective"] = {"select": {"name": objective}}
        if priority:
            props["Приоритет"] = {"select": {"name": priority}}
        if responsible:
            props["Ответственный"] = {"select": {"name": responsible}}
        if week:
            props["Неделя"] = {"rich_text": [{"text": {"content": week}}]}
        if deadline:
            props["Дедлайн"] = {"date": {"start": deadline}}
        if done_when:
            props["Что считается готово"] = {"rich_text": [{"text": {"content": done_when[:2000]}}]}
        if bitrix_task:
            props["Bitrix Task"] = {"rich_text": [{"text": {"content": bitrix_task}}]}

        # тело страницы: ожидаемый результат, критерий успеха, обоснование
        children = []
        for label, val in (("🎯 Ожидаемый результат", expected_result),
                           ("✅ Критерий успеха", done_when),
                           ("💭 Обоснование", reasoning)):
            if val:
                children.append({"object": "block", "type": "paragraph", "paragraph": {
                    "rich_text": [{"text": {"content": f"{label}: {val}"[:1900]}}]}})

        body: dict[str, Any] = {"parent": {"database_id": INBOX_DB_ID}, "properties": props}
        if children:
            body["children"] = children
        data = self._request("POST", "/pages", body)
                             {"parent": {"database_id": INBOX_DB_ID}, "properties": props})
        return {"id": data.get("id"), "url": data.get("url")}


# ── вспомогательные преобразователи ──────────────────────────────────────────

def _plain(rich: list | None) -> str:
    return "".join(r.get("plain_text", "") for r in (rich or []))


def _block_text(block: dict) -> str:
    t = block.get("type")
    node = block.get(t) or {}
    rich = node.get("rich_text")
    if rich is not None:
        prefix = "- " if t in ("bulleted_list_item", "numbered_list_item") else ""
        if t.startswith("heading"):
            prefix = "# "
        return prefix + _plain(rich)
    return ""


def _simplify_page(page: dict) -> dict:
    props = page.get("properties", {})
    out: dict[str, Any] = {"id": page.get("id"), "url": page.get("url")}
    for name, p in props.items():
        t = p.get("type")
        if t == "title":
            out[name] = _plain(p.get("title"))
        elif t == "rich_text":
            out[name] = _plain(p.get("rich_text"))
        elif t == "select":
            out[name] = (p.get("select") or {}).get("name")
        elif t == "multi_select":
            out[name] = [o.get("name") for o in p.get("multi_select", [])]
        elif t == "date":
            out[name] = (p.get("date") or {}).get("start")
        elif t in ("created_time", "last_edited_time"):
            out[name] = p.get(t)
        elif t == "people":
            out[name] = [o.get("name") for o in p.get("people", [])]
    return out
