"""
Тонкая обёртка над REST API Bitrix24 (через входящий вебхук).

Вебхук выглядит так:
    https://ВАШ-ПОРТАЛ.bitrix24.ru/rest/1/ABCDEF1234567890/

Документация методов: https://apidocs.bitrix24.ru/
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import requests


class BitrixError(RuntimeError):
    pass


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = 30):
        if not webhook_url:
            raise BitrixError("Не задан BITRIX_WEBHOOK_URL")
        # гарантируем завершающий слэш
        self.base = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Один вызов метода. Возвращает содержимое поля result."""
        url = self.base + method + ".json"
        params = params or {}
        for attempt in range(6):
            resp = self.session.post(url, json=params, timeout=self.timeout)
            if resp.status_code in (503, 429):
                time.sleep(1.5 * (attempt + 1))
                continue
            data = resp.json()
            err = data.get("error")
            if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"):
                time.sleep(1.5 * (attempt + 1))  # лимит запросов — ждём и повторяем
                continue
            if err:
                raise BitrixError(f"{method}: {err} — {data.get('error_description')}")
            return data.get("result")
        raise BitrixError(f"{method}: превышено число попыток (throttling)")

    def call_list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        max_items: int = 500,
    ) -> list[Any]:
        """Постраничный обход list-метода (crm.deal.list, tasks.task.list и т.п.)."""
        params = dict(params or {})
        out: list[Any] = []
        start = 0
        url = self.base + method + ".json"
        while True:
            params["start"] = start
            for attempt in range(6):
                resp = self.session.post(url, json=params, timeout=self.timeout)
                data = resp.json()
                err = data.get("error")
                if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT") or resp.status_code in (503, 429):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if err:
                    raise BitrixError(f"{method}: {err} — {data.get('error_description')}")
                break
            else:
                raise BitrixError(f"{method}: превышено число попыток (throttling)")
            chunk = data.get("result") or []
            # некоторые методы (tasks.task.list) кладут список внутрь словаря
            if isinstance(chunk, dict) and "tasks" in chunk:
                chunk = chunk["tasks"]
            out.extend(chunk)
            if len(out) >= max_items:
                return out[:max_items]
            nxt = data.get("next")
            if not nxt:
                return out
            start = nxt
            time.sleep(0.2)  # бережём лимиты


def chunked(seq: Iterable[Any], size: int) -> Iterable[list[Any]]:
    buf: list[Any] = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
