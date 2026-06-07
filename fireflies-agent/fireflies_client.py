"""
Клиент Fireflies.ai GraphQL API.

Авторизация — личный API-ключ (Fireflies → Settings → Developer Settings → API Key),
передаётся как Bearer-токен. Документация: https://docs.fireflies.ai/graphql-api
"""

from __future__ import annotations

import time
from typing import Any

import requests

ENDPOINT = "https://api.fireflies.ai/graphql"

# Поля одной встречи, которые мы запрашиваем. summary даёт нам короткое резюме,
# ключевые слова и action items — этого достаточно для анализа без выгрузки
# полной стенограммы (она тяжёлая и редко нужна целиком).
_TRANSCRIPT_FIELDS = """
  id
  title
  dateString
  duration
  organizer_email
  host_email
  participants
  meeting_attendees { displayName email name }
  summary {
    short_summary
    overview
    keywords
    action_items
    bullet_gist
    meeting_type
  }
"""

_LIST_QUERY = f"""
query Transcripts($fromDate: DateTime, $toDate: DateTime, $limit: Int, $skip: Int) {{
  transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip) {{
    {_TRANSCRIPT_FIELDS}
  }}
}}
"""


class Fireflies:
    def __init__(self, api_key: str, timeout: int = 60):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                resp = self.session.post(
                    ENDPOINT,
                    json={"query": query, "variables": variables},
                    timeout=self.timeout,
                )
                # 429 / 5xx — ждём и пробуем снова
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                data = resp.json()
                if data.get("errors"):
                    raise RuntimeError(f"Fireflies GraphQL error: {data['errors']}")
                return data["data"]
            except (requests.RequestException, ValueError) as e:  # noqa: PERF203
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Fireflies API недоступен: {last_err}")

    def list_transcripts(
        self,
        from_iso: str,
        to_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Все встречи в окне [from_iso, to_iso] (ISO 8601), с пагинацией."""
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            page = self._post(_LIST_QUERY, {
                "fromDate": from_iso,
                "toDate": to_iso,
                "limit": min(limit, 50),
                "skip": skip,
            }).get("transcripts") or []
            out.extend(page)
            if len(page) < min(limit, 50):
                break
            skip += len(page)
            if skip >= 300:  # предохранитель
                break
        return out

    def get_detail(self, transcript_id: str) -> dict[str, Any]:
        """Полная стенограмма одной встречи: реплики (кто что сказал) + summary."""
        data = self._post(_DETAIL_QUERY, {"id": transcript_id})
        return data.get("transcript") or {}


_DETAIL_QUERY = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    dateString
    duration
    organizer_email
    participants
    meeting_attendees { displayName email }
    summary { short_summary overview action_items keywords }
    sentences { text speaker_name }
  }
}
"""
