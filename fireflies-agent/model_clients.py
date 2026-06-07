"""
Единый интерфейс к трём ИИ-провайдерам для «Совета директоров».

ask(provider, system, user, env) -> (text, provider_used)

Если ключа провайдера нет в .env — запрос автоматически уходит в Claude
(graceful degradation), и provider_used отражает реально ответившую модель.

Модели задаются в .env и легко меняются на актуальные:
  MODEL=claude-opus-4-8            (Claude, уже есть)
  OPENAI_MODEL=gpt-4o             (OpenAI)
  GEMINI_MODEL=gemini-2.5-flash     (Google)
"""

from __future__ import annotations

import anthropic
import requests

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def ask(provider: str, system: str, user: str, env: dict,
        max_tokens: int = 1200) -> tuple[str, str]:
    """Спрашивает указанного провайдера. Возвращает (текст, имя_провайдера)."""
    provider = (provider or "claude").lower()
    try:
        if provider == "openai" and env.get("OPENAI_API_KEY"):
            return _openai(system, user, env, max_tokens), "GPT"
        if provider == "gemini" and env.get("GEMINI_API_KEY"):
            return _gemini(system, user, env, max_tokens), "Gemini"
    except Exception as e:  # noqa: BLE001 — провайдер упал → откат на Claude
        return _claude(system, user, env, max_tokens) + f"\n\n<i>(⚠️ {provider} недоступен: {e}; ответил Claude)</i>", "Claude*"
    # claude или провайдер без ключа
    return _claude(system, user, env, max_tokens), "Claude"


def _claude(system: str, user: str, env: dict, max_tokens: int) -> str:
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    resp = client.messages.create(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _openai(system: str, user: str, env: dict, max_tokens: int) -> str:
    model = env.get("OPENAI_MODEL", "gpt-4o")
    r = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=90)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(data.get("error", {}).get("message", r.text[:200]))
    return data["choices"][0]["message"]["content"].strip()


def _gemini(system: str, user: str, env: dict, max_tokens: int) -> str:
    model = env.get("GEMINI_MODEL", "gemini-2.5-flash")
    r = requests.post(
        GEMINI_URL.format(model=model),
        params={"key": env["GEMINI_API_KEY"]},
        headers={"Content-Type": "application/json"},
        json={"system_instruction": {"parts": [{"text": system}]},
              "contents": [{"role": "user", "parts": [{"text": user}]}],
              "generationConfig": {"maxOutputTokens": max_tokens}},
        timeout=90)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError((data.get("error") or {}).get("message", r.text[:200]))
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or [{}]
    return "".join(p.get("text", "") for p in parts).strip()
