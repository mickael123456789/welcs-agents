#!/usr/bin/env python3
"""
Единый диалоговый бот (Telegram) с кнопками-источниками: 🔥 Fireflies / 📊 Bitrix.

Один бот @AuditorWelcs_bot обслуживает оба агента. Поскольку Telegram разрешает
слушать входящие только одному процессу на токен — это ЕДИНСТВЕННЫЙ слушатель.

UX:
  Внизу чата две постоянные кнопки. Жмёшь нужный источник → пишешь вопрос →
  бот отвечает из выбранного источника:
    🔥 Fireflies — вопросы о встречах команды (ищет в Fireflies, читает стенограммы).
    📊 Bitrix   — вопросы по CRM/задачам/чатам (читает Bitrix24 через вебхук).
  Выбор источника запоминается до следующего нажатия.

Постоянный процесс (long-polling). Демон: com.welcs.fireflies-bot (KeepAlive).
Запуск вручную:  ../bitrix-agent/.venv/bin/python bot.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic
import requests

HERE = Path(__file__).parent
BITRIX_DIR = HERE.parent / "bitrix-agent"
sys.path.insert(0, str(BITRIX_DIR))  # чтобы импортировать bitrix_client

OFFSET_FILE = HERE / ".bot_offset"
STATE_FILE = HERE / ".bot_state.json"   # {chat_id: "fireflies"|"bitrix"|"priorities"}
PENDING_FILE = HERE / ".bot_pending.json"  # {chat_id: draft} — черновик, ждущий ✅
BOARD_CFG_FILE = HERE / ".bot_board_cfg.json"  # {chat_id: {role_key: provider}} — состав совета
TEAM_FILE = HERE / "team_access.json"   # {"users": {chat_id: {name, modes:[...]}}} — доступ команды
TG = "https://api.telegram.org/bot{token}/{method}"

# Кому отвечать в текущей итерации — тому, кто написал. Бот однопоточный (long-polling),
# поэтому одной модульной переменной достаточно; для рассылок (run.py) не используется.
_REPLY_CHAT: str | None = None
# Клавиатура для текущего ответа (у ограниченных пользователей — только их кнопки).
_REPLY_KEYBOARD: dict | None = None

BTN_FF = "🔥 Fireflies"
BTN_BX = "📊 Bitrix"
BTN_GO = "🎯 Цели"
BTN_BD = "🧠 Совет"
BTN_MO = "🌅 Утро"
BTN_NO = "📝 Notion"
KEYBOARD = {
    "keyboard": [[{"text": BTN_FF}, {"text": BTN_BX}], [{"text": BTN_GO}, {"text": BTN_BD}],
                 [{"text": BTN_MO}, {"text": BTN_NO}]],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Выбери режим кнопкой, потом напиши",
}

# inline-кнопки под черновиком записи (режим 🎯 Цели)
DRAFT_KEYBOARD = {"inline_keyboard": [[
    {"text": "✅ Записать", "callback_data": "go_ok"},
    {"text": "✏️ Править", "callback_data": "go_edit"},
    {"text": "❌ Отмена", "callback_data": "go_cancel"},
]]}

# inline-кнопки под решением совета (режим 🧠 Совет)
BOARD_KEYBOARD = {"inline_keyboard": [[
    {"text": "💾 Записать решение в Notion", "callback_data": "bd_save"},
    {"text": "✖️ Не надо", "callback_data": "bd_dismiss"},
]]}

# ── режимы и доступ ───────────────────────────────────────────────────────────
# Все режимы бота и соответствие «режим → кнопка».
MODE_BTN = {"fireflies": BTN_FF, "bitrix": BTN_BX, "priorities": BTN_GO,
            "board": BTN_BD, "morning": BTN_MO, "notion": BTN_NO}
ALL_MODES = set(MODE_BTN)
# Текст/команда → режим (для распознавания выбора режима).
MODE_ALIASES = {
    "fireflies": ("fireflies", "/fireflies"),
    "bitrix": ("bitrix", "/bitrix"),
    "priorities": ("цели", "/goals", "/priorities"),
    "board": ("совет", "/board", "/sovet"),
    "morning": ("утро", "/morning", "/utro"),
    "notion": ("notion", "/notion", "ноушн"),
}


def _team_access() -> dict:
    """Доступ команды из team_access.json: {chat_id: {name, modes:[...]}}.

    Файл лежит в репозитории (не в .gitignore), поэтому переживает перезапуски
    облачного процесса — в отличие от .bot_* состояния.
    """
    try:
        data = json.loads(TEAM_FILE.read_text(encoding="utf-8"))
        return {str(k): v for k, v in (data.get("users") or {}).items()}
    except (OSError, ValueError):
        return {}


def _allowed_modes(chat_id: str, owner: str) -> set:
    """Какие режимы доступны чату. Владелец — все; команда — из конфига; иначе — пусто."""
    if chat_id == owner:
        return set(ALL_MODES)
    u = _team_access().get(chat_id)
    return {m for m in (u.get("modes") or []) if m in ALL_MODES} if u else set()


def _mode_from_text(text: str) -> str | None:
    """Распознать выбор режима по тексту кнопки или команде."""
    low = text.lower()
    for mode, btn in MODE_BTN.items():
        if text == btn:
            return mode
    for mode, aliases in MODE_ALIASES.items():
        if low in aliases:
            return mode
    return None


def _keyboard_for(modes: set) -> dict:
    """Клавиатура только из разрешённых пользователю кнопок (по 2 в ряд)."""
    if modes >= ALL_MODES:
        return KEYBOARD
    btns = [{"text": MODE_BTN[m]} for m in MODE_BTN if m in modes]  # стабильный порядок
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True,
            "input_field_placeholder": "Выбери режим кнопкой, потом напиши"}


# ── окружение: сливаем .env обоих агентов ─────────────────────────────────────

def _read_env() -> dict:
    env: dict[str, str] = {}
    for path in (BITRIX_DIR / ".env", HERE / ".env"):  # fireflies перекрывает общее
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    # Переменные окружения имеют приоритет над .env (нужно для облака/GitHub Actions,
    # где файлов .env нет, а секреты приходят как env vars).
    for k in ("FIREFLIES_API_KEY", "ANTHROPIC_API_KEY", "MODEL", "NOTIFY_CHANNEL",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BITRIX_WEBHOOK_URL",
              "NOTION_API_KEY", "ME_EMAILS", "TEAM_DOMAINS", "TEAM_EMAILS",
              "GEMINI_API_KEY", "OPENAI_API_KEY", "BOT_MAX_RUNTIME"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# ════════════════════════════ FIREFLIES Q&A ══════════════════════════════════

FF_SYSTEM = """\
Ты — ассистент руководителя компании Welcs. Отвечаешь на его вопросы о встречах \
команды, записанных в Fireflies (внутренние встречи, собеседования с кандидатами, \
переговоры с партнёрами/инвесторами/клиентами).

Инструменты:
- search_meetings(query, participant_email, days) — ПОИСК встреч по теме/названию или \
  участнику по ВСЕЙ истории Fireflies (не только последние). Это главный инструмент: \
  если в вопросе есть тема, имя человека, компания, проект или ключевое слово — \
  начинай отсюда. days оставляй пустым, чтобы искать по всей истории.
- list_recent_meetings(days) — список последних встреч за период. Используй ТОЛЬКО когда \
  вопрос именно про «последние / недавние» встречи без конкретной темы.
- get_meeting_transcript(meeting_id) — полная стенограмма встречи + action items.

ВАЖНО: не ограничивайся последними встречами. Если по теме ничего не нашлось — попробуй \
другие ключевые слова (синонимы, имя участника, название компании) и поиск без ограничения \
по дате. Только если после нескольких попыток пусто — скажи, что встреча не найдена.

Отвечай по-русски, кратко, по делу. Опирайся только на содержимое встреч. Если расшифровки \
нет или встреча не найдена — скажи прямо. Указывай название и дату встречи при ссылке."""

FF_TOOLS = [
    {"name": "search_meetings",
     "description": "Поиск встреч по теме/названию или участнику по ВСЕЙ истории Fireflies. "
                    "Главный инструмент поиска — используй, когда в вопросе есть тема, "
                    "ключевое слово, имя человека или компания. Возвращает встречи с резюме.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Тема/ключевое слово/название встречи "
                   "(ищет по заголовку встречи)."},
         "participant_email": {"type": "string", "description": "E-mail участника (необязательно)."},
         "days": {"type": "integer", "description": "Ограничить последними N днями "
                  "(необязательно; по умолчанию ищет по всей истории)."}}}},
    {"name": "list_recent_meetings",
     "description": "Список последних встреч за N дней с короткими резюме и участниками. "
                    "Только для вопросов про недавние встречи без конкретной темы.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "За сколько дней (по умолчанию 30, макс 365)."}}}},
    {"name": "get_meeting_transcript",
     "description": "Полная стенограмма встречи по id (реплики по спикерам, action items).",
     "input_schema": {"type": "object", "properties": {"meeting_id": {"type": "string"}},
                      "required": ["meeting_id"]}},
]


def _iso(days_ago: int) -> str:
    d = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ff_format(items: list) -> str:
    out = []
    for t in items:
        s = t.get("summary") or {}
        out.append({"id": t.get("id"), "title": t.get("title"), "date": t.get("dateString"),
                    "duration_min": round(t.get("duration") or 0, 1),
                    "participants": t.get("participants"),
                    "short_summary": s.get("short_summary") or s.get("overview"),
                    "action_items": s.get("action_items")})
    return json.dumps(out, ensure_ascii=False)


def _ff_list(ff, days: int) -> str:
    days = max(1, min(int(days or 30), 365))
    return _ff_format(ff.list_transcripts(_iso(days), _iso(0), limit=50))


def _ff_search(ff, query: str, participant_email: str = "", days: int = 0) -> str:
    """Поиск по всей истории Fireflies по теме/названию или участнику."""
    query = (query or "").strip()
    participant_email = (participant_email or "").strip()
    if not query and not participant_email:
        return json.dumps({"error": "нужно указать тему (query) или e-mail участника"},
                          ensure_ascii=False)
    days = min(int(days), 365) if days else None
    items = ff.search_transcripts(title=query or None,
                                  participant_email=participant_email or None,
                                  days=days, limit=50)
    if not items:
        return json.dumps({"found": 0, "query": query, "participant_email": participant_email,
                           "note": "По этому запросу встреч не найдено. Попробуй другое "
                                   "ключевое слово / синоним / имя участника."},
                          ensure_ascii=False)
    return _ff_format(items)


def _ff_transcript(ff, mid: str) -> str:
    t = ff.get_detail(mid)
    if not t:
        return json.dumps({"error": "встреча не найдена"}, ensure_ascii=False)
    sents = t.get("sentences") or []
    if not sents:
        return json.dumps({"id": t.get("id"), "title": t.get("title"), "date": t.get("dateString"),
                           "note": "Стенограммы нет (встреча без записи Fireflies).",
                           "summary": t.get("summary")}, ensure_ascii=False)
    body = "\n".join(f"{x.get('speaker_name') or '?'}: {x.get('text')}" for x in sents)
    if len(body) > 60_000:
        body = body[:60_000] + "\n…(обрезано)"
    return json.dumps({"id": t.get("id"), "title": t.get("title"), "date": t.get("dateString"),
                       "summary": t.get("summary"), "transcript": body}, ensure_ascii=False)


def answer_fireflies(question: str, env: dict) -> str:
    from fireflies_client import Fireflies
    ff = Fireflies(env["FIREFLIES_API_KEY"])
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        resp = client.messages.create(
            model=model, max_tokens=3000,
            system=[{"type": "text", "text": FF_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=FF_TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "Не нашёл, что ответить."
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                if b.name == "search_meetings":
                    out = _ff_search(ff, b.input.get("query", ""),
                                     b.input.get("participant_email", ""),
                                     b.input.get("days", 0))
                elif b.name == "list_recent_meetings":
                    out = _ff_list(ff, b.input.get("days", 30))
                else:
                    out = _ff_transcript(ff, b.input.get("meeting_id", ""))
            except Exception as e:  # noqa: BLE001
                out = json.dumps({"error": str(e)}, ensure_ascii=False)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "Слишком долго искал — переформулируй вопрос, пожалуйста."


# ════════════════════════════ BITRIX Q&A ═════════════════════════════════════

BX_SYSTEM = """\
Ты — ассистент руководителя (админа Bitrix24 компании Welcs — управление арендой \
недвижимости, Costa Brava). Отвечаешь на вопросы по CRM, задачам, сделкам, лидам, \
сотрудникам и чатам, читая Bitrix24 через инструмент bitrix_query.

Полезные методы Bitrix (только чтение):
- crm.deal.list / crm.lead.list / crm.contact.list / crm.company.list — фильтр через {"filter": {...}}, \
  поиск по названию: {"filter": {"%TITLE": "слово"}}; выбор полей через {"select": ["ID","TITLE",...]}.
- crm.deal.get / crm.lead.get (params {"id": N}).
- tasks.task.list — задачи; фильтры {"filter": {"REAL_STATUS": 2}} (2 ожидает,5 завершена), \
  поиск {"filter": {"%TITLE": "слово"}}, просрочка {"filter": {"<DEADLINE": "ISO-дата"}}. Ключи в camelCase.
- user.get — сотрудники.
- crm.status.list / crm.dealcategory.stage.list — справочники стадий.

Операционка (уборки, заезды, инциденты) живёт в ЗАДАЧАХ с названиями вроде LIMPIEZA, \
Reclamo, CHECK IN. Детали часто в DESCRIPTION задачи.

СООБЩЕНИЯ И ЧАТЫ (открытые линии — WhatsApp через Wazzup24, линии RESERVAS, PROPIETARIO, \
Online chat WELCS ES/FR/DE/EN). Ты МОЖЕШЬ их читать через bitrix_query:
- im.recent.list с params {"SKIP_OPENLINES": "N"} — последние диалоги. Диалог открытой линии \
  определяется по chat.entity_type == "LINES" (верхний type обманчиво "chat"); заголовок вида \
  "Имя клиента - RESERVAS/PROPIETARIO" или "… WELCS 197".
- im.dialog.messages.get с params {"DIALOG_ID": "chatNNNN", "LIMIT": 50} — сами сообщения диалога.
Для вопросов «что сейчас по сообщениям / что висит без ответа / кто медленно отвечает» используй \
готовый инструмент messages_overview — он сразу даёт SLA: кто ждёт ответа и сколько минут, \
скорость операторов. Это быстрее и точнее, чем собирать вручную.

Делай несколько вызовов, если нужно. Отвечай по-русски, кратко, с конкретикой (id, названия, \
суммы, сроки, минуты ожидания). Если данных нет — скажи прямо. Не выдумывай."""

# методы только для чтения: разрешаем *.list/.get/.fields/.stages, запрещаем мутации
_BX_ALLOW = re.compile(r"\.(list|get|fields|stages)$", re.I)
_BX_DENY = re.compile(r"\.(add|update|delete|set|register|unregister|import)\b", re.I)

BX_TOOLS = [
    {"name": "bitrix_query",
     "description": "Вызов read-only метода Bitrix24 REST. method — например 'crm.deal.list', "
                    "'tasks.task.list', 'user.get', 'im.recent.list', 'im.dialog.messages.get'. "
                    "params — словарь параметров метода.",
     "input_schema": {"type": "object", "properties": {
         "method": {"type": "string"},
         "params": {"type": "object", "description": "Параметры метода (filter/select/order/id/DIALOG_ID и т.п.)."}},
         "required": ["method"]}},
    {"name": "messages_overview",
     "description": "Готовая сводка по сообщениям открытых линий за N часов: кто из клиентов ждёт "
                    "ответа и сколько минут, какие отвечены и за сколько, скорость операторов (SLA). "
                    "Используй для вопросов про сообщения/чаты/без ответа/скорость ответа.",
     "input_schema": {"type": "object", "properties": {
         "hours": {"type": "integer", "description": "Окно в часах (по умолчанию 24, макс 168)."}}}},
]


def _bx_call(bx, method: str, params: dict) -> str:
    method = (method or "").strip()
    if _BX_DENY.search(method) or not _BX_ALLOW.search(method):
        return json.dumps({"error": f"метод {method!r} запрещён (только чтение: *.list/.get/.fields)"},
                          ensure_ascii=False)
    params = params or {}
    try:
        if method.endswith(".list"):
            res = bx.call_list(method, params, max_items=50)
        else:
            res = bx.call(method, params)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    text = json.dumps(res, ensure_ascii=False)
    if len(text) > 40_000:
        text = text[:40_000] + "…(обрезано)"
    return text


def _bx_messages_overview(bx, hours=24) -> str:
    """Сводка по сообщениям открытых линий: SLA (кто без ответа, сколько ждёт) + операторы."""
    from collect import collect_dialogs, build_user_map, now_aware
    from analytics import open_line_sla, operator_scorecard
    try:
        hours = max(1, min(int(hours or 24), 168))
    except (TypeError, ValueError):
        hours = 24
    errors: list = []
    users = build_user_map(bx, errors)
    dlg = collect_dialogs(bx, users, hours, 40, 30, errors)
    data = {"open_lines": dlg.get("open_lines", []), "team_chats": dlg.get("team_chats", [])}
    sla = open_line_sla(data, now_aware())

    # прикладываем последние реплики каждого диалога, чтобы бот сразу видел,
    # О ЧЁМ сообщения (а не только время ожидания)
    by_id = {d.get("dialog_id"): d.get("messages", []) for d in data["open_lines"]}

    def _tail(item):
        msgs = by_id.get(item.get("dialog_id")) or []
        return [{"author": m.get("author"), "date": m.get("date"), "text": (m.get("text") or "")[:400]}
                for m in msgs[-8:]]

    for it in sla["unanswered"]:
        it["recent_messages"] = _tail(it)
    for it in sla["answered"][:10]:
        it["recent_messages"] = _tail(it)

    return json.dumps({
        "window_hours": hours,
        "open_line_dialogs": len(data["open_lines"]),
        "unanswered": sla["unanswered"],
        "answered_recent": sla["answered"][:10],
        "operators": operator_scorecard(data),
        "notes": errors,
        "hint": "recent_messages у каждого диалога — это реальные последние реплики; "
                "используй их, чтобы пересказать О ЧЁМ сообщение. Для полной переписки вызови "
                "im.dialog.messages.get с DIALOG_ID из поля dialog_id.",
    }, ensure_ascii=False)


def answer_bitrix(question: str, env: dict) -> str:
    if not env.get("BITRIX_WEBHOOK_URL"):
        return "⚠️ Bitrix не настроен: нет BITRIX_WEBHOOK_URL в .env Bitrix-агента."
    from bitrix_client import Bitrix
    bx = Bitrix(env["BITRIX_WEBHOOK_URL"])
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        resp = client.messages.create(
            model=model, max_tokens=3000,
            system=[{"type": "text", "text": BX_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=BX_TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "Не нашёл, что ответить."
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if b.name == "messages_overview":
                try:
                    out = _bx_messages_overview(bx, b.input.get("hours", 24))
                except Exception as e:  # noqa: BLE001
                    out = json.dumps({"error": str(e)}, ensure_ascii=False)
            else:
                out = _bx_call(bx, b.input.get("method", ""), b.input.get("params") or {})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "Слишком долго искал — переформулируй вопрос, пожалуйста."


# ════════════════════════════ NOTION Q&A ═════════════════════════════════════

NX_SYSTEM = """\
Ты — ассистент руководителя компании Welcs. Отвечаешь на вопросы про рабочее \
пространство Notion: какие страницы и базы подключены к боту, что в них, статусы \
задач и целей.

ВАЖНО про «подключение». Бот видит в Notion ТОЛЬКО то, что явно расшарено его \
интеграции (в Notion: страница/база → ••• → Connections → выбрать интеграцию). \
Инструмент notion_query с action="search" и пустым query возвращает полный список \
доступных интеграции страниц и баз — именно по нему проверяй, «подключена ли ещё \
одна страница». Если нужной страницы нет в выдаче search — значит, её ещё НЕ \
расшарили интеграции, и об этом надо прямо сказать (и подсказать открыть доступ \
через ••• → Connections).

Инструмент notion_query:
- action="search", query="…" — найти страницы/базы (пустой query = показать все доступные).
- action="read_page", page_id="…" — прочитать текст страницы.
- action="query_database", database_id="…" — строки базы.

Отвечай по-русски, кратко и по делу, указывай названия. Не выдумывай: если объекта \
нет в выдаче — так и говори."""

NX_TOOLS = [
    {"name": "notion_query",
     "description": "Чтение Notion (только то, что расшарено интеграции): "
                    "search — список доступных страниц/баз; read_page — текст страницы; "
                    "query_database — строки базы.",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["search", "read_page", "query_database"]},
         "query": {"type": "string", "description": "Текст для action=search (пусто = всё доступное)."},
         "only": {"type": "string", "enum": ["page", "database"],
                  "description": "Фильтр типа объекта для search (необязательно)."},
         "page_id": {"type": "string", "description": "Для action=read_page."},
         "database_id": {"type": "string", "description": "Для action=query_database."}},
         "required": ["action"]}},
]


def _nx_call(nx, inp: dict) -> str:
    action = (inp.get("action") or "").strip()
    try:
        if action == "search":
            res: Any = nx.search(inp.get("query", "") or "", only=inp.get("only"))
        elif action == "read_page":
            res = {"text": nx.get_page_text(inp.get("page_id", ""))}
        elif action == "query_database":
            res = nx.query_database(inp.get("database_id", ""))
        else:
            res = {"error": f"неизвестное действие {action!r}"}
    except Exception as e:  # noqa: BLE001
        res = {"error": str(e)}
    text = json.dumps(res, ensure_ascii=False)
    return text[:40_000] + ("…(обрезано)" if len(text) > 40_000 else "")


def answer_notion(question: str, env: dict) -> str:
    if not env.get("NOTION_API_KEY"):
        return "⚠️ Notion не настроен: нет NOTION_API_KEY в .env."
    from notion_client import Notion
    nx = Notion(env["NOTION_API_KEY"])
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    messages = [{"role": "user", "content": question}]
    for _ in range(8):
        resp = client.messages.create(
            model=model, max_tokens=3000,
            system=[{"type": "text", "text": NX_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=NX_TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "Не нашёл, что ответить."
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": _nx_call(nx, b.input)})
        messages.append({"role": "user", "content": results})
    return "Слишком долго искал — переформулируй вопрос, пожалуйста."


# ════════════════════════════ Telegram loop ══════════════════════════════════

def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False))


def _pending() -> dict:
    try:
        return json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_pending(p: dict) -> None:
    PENDING_FILE.write_text(json.dumps(p, ensure_ascii=False))


def _board_cfg() -> dict:
    try:
        return json.loads(BOARD_CFG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_board_cfg(c: dict) -> None:
    BOARD_CFG_FILE.write_text(json.dumps(c, ensure_ascii=False))


def _edit_inline(env: dict, message_id: int, text: str, inline_markup: dict) -> None:
    """Редактирует ранее отправленное сообщение (для меню настройки совета)."""
    from notify_telegram import _md_to_tg_html
    requests.post(TG.format(token=env["TELEGRAM_BOT_TOKEN"], method="editMessageText"),
                  json={"chat_id": _REPLY_CHAT or env["TELEGRAM_CHAT_ID"], "message_id": message_id,
                        "text": _md_to_tg_html(text), "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": json.dumps(inline_markup)}, timeout=30)


def _send_inline(env: dict, text: str, inline_markup: dict) -> None:
    """Отправка сообщения с inline-кнопками (для черновика записи)."""
    from notify_telegram import _md_to_tg_html
    token = env["TELEGRAM_BOT_TOKEN"]
    payload = {"chat_id": _REPLY_CHAT or env["TELEGRAM_CHAT_ID"], "text": _md_to_tg_html(text),
               "parse_mode": "HTML", "disable_web_page_preview": True,
               "reply_markup": json.dumps(inline_markup)}
    r = requests.post(TG.format(token=token, method="sendMessage"), json=payload, timeout=30).json()
    if not r.get("ok"):
        payload["text"] = re.sub(r"<[^>]+>", "", text)
        payload.pop("parse_mode", None)
        requests.post(TG.format(token=token, method="sendMessage"), json=payload, timeout=30)


def _answer_callback(env: dict, callback_id: str, text: str = "") -> None:
    requests.post(TG.format(token=env["TELEGRAM_BOT_TOKEN"], method="answerCallbackQuery"),
                  json={"callback_query_id": callback_id, "text": text}, timeout=10)


def _handle_callback(env: dict, cq: dict, allowed: str) -> None:
    """Нажатие inline-кнопки под черновиком записи (✅/✏️/❌)."""
    global _REPLY_CHAT
    chat_id = str((((cq.get("message") or {}).get("chat")) or {}).get("id"))
    _REPLY_CHAT = chat_id  # отвечаем тому, кто нажал кнопку
    cid = cq.get("id")
    data = cq.get("data") or ""
    if chat_id != allowed and chat_id not in _team_access():
        _answer_callback(env, cid)
        return
    pend = _pending()
    draft = pend.get(chat_id)

    # ── настройка состава совета (🧠) ────────────────────────────────────────
    if data.startswith("cfg:"):
        from board import default_config, cycle_provider, render_menu
        action = data.split(":", 1)[1]
        cfgs = _board_cfg()
        cfg = cfgs.get(chat_id) or default_config()
        msg_id = ((cq.get("message") or {}).get("message_id"))
        if action == "done":
            cfgs[chat_id] = cfg; _save_board_cfg(cfgs)
            from board import effective_advisors
            n = len(effective_advisors(cfg))
            _answer_callback(env, cid, "Состав сохранён")
            if msg_id:
                _edit_inline(env, msg_id, f"🧠 Состав совета сохранён: <b>{n}</b> ролей. "
                                          "Теперь задай стратегический вопрос 👇", {"inline_keyboard": []})
            return
        if action == "reset":
            cfg = default_config()
        else:  # тап по роли — переключаем модель по кругу
            cfg[action] = cycle_provider(cfg.get(action, "claude"))
        cfgs[chat_id] = cfg; _save_board_cfg(cfgs)
        _answer_callback(env, cid)
        if msg_id:
            menu_text, menu_kb = render_menu(cfg)
            _edit_inline(env, msg_id, menu_text, menu_kb)
        return

    if data == "go_cancel":
        pend.pop(chat_id, None); _save_pending(pend)
        _answer_callback(env, cid, "Отменено")
        _send(env, "❌ Отменил, ничего не записал.")
        return
    if data == "go_edit":
        _answer_callback(env, cid, "Ок, правь")
        _send(env, "✏️ Напиши, что поправить или сформулируй заново — пересоберу черновик.")
        return
    if data == "go_ok":
        if not draft:
            _answer_callback(env, cid, "Черновик не найден")
            _send(env, "Черновик потерялся — пришли мысль заново.")
            return
        _answer_callback(env, cid, "Записываю…")
        from priorities import commit_draft
        try:
            report = commit_draft(draft, env)
        except Exception as e:  # noqa: BLE001
            report = f"⚠️ Ошибка записи: {e}"
        pend.pop(chat_id, None); _save_pending(pend)
        _send(env, "🎯 " + report)
        return

    # ── решение совета директоров (🧠) ──────────────────────────────────────
    if data == "bd_dismiss":
        pend.pop(chat_id + ":board", None); _save_pending(pend)
        _answer_callback(env, cid, "Ок")
        _send(env, "✖️ Не стал записывать решение.")
        return
    if data == "bd_save":
        decision = pend.get(chat_id + ":board")
        if not decision:
            _answer_callback(env, cid, "Решение не найдено")
            _send(env, "Решение потерялось — спроси совет заново.")
            return
        _answer_callback(env, cid, "Записываю…")
        try:
            from notion_client import Notion
            n = Notion(env["NOTION_API_KEY"])
            res = n.create_inbox_entry(
                title=decision.get("title", "Решение совета"),
                tip="Цель", objective=decision.get("objective"),
                priority="High", responsible="Михаил (сам)",
                done_when=(decision.get("recommendation", "") +
                           ("\n\nПервый шаг: " + decision["next_step"] if decision.get("next_step") else "")))
            msg = f"💾 Записал решение: <a href=\"{res['url']}\">{decision.get('title')}</a>"
        except Exception as e:  # noqa: BLE001
            msg = f"⚠️ Не удалось записать решение: {e}"
        pend.pop(chat_id + ":board", None); _save_pending(pend)
        _send(env, msg)
        return
    _answer_callback(env, cid)


def _send(env: dict, text: str, keyboard: bool = True) -> None:
    """Отправка с конвертацией markdown→HTML и постоянной клавиатурой."""
    from notify_telegram import _md_to_tg_html, _split
    token = env["TELEGRAM_BOT_TOKEN"]
    chat_id = _REPLY_CHAT or env["TELEGRAM_CHAT_ID"]
    chunks = _split(_md_to_tg_html(text))
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if keyboard and i == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(_REPLY_KEYBOARD or KEYBOARD)
        try:
            r = requests.post(TG.format(token=token, method="sendMessage"),
                              json=payload, timeout=30).json()
            if not r.get("ok"):  # повтор без разметки
                payload["text"] = re.sub(r"<[^>]+>", "", chunk)
                payload.pop("parse_mode", None)
                requests.post(TG.format(token=token, method="sendMessage"),
                              json=payload, timeout=30)
        except (requests.RequestException, ValueError) as e:
            # сетевой сбой при отправке не должен ронять демон
            print(f"⚠️ _send: {e}", file=sys.stderr)
        time.sleep(0.3)


def _get_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def main() -> int:
    global _REPLY_CHAT, _REPLY_KEYBOARD
    env = _read_env()
    for k in ("FIREFLIES_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not env.get(k):
            print(f"❌ Не задан {k} в .env", file=sys.stderr)
            return 2

    token = env["TELEGRAM_BOT_TOKEN"]
    allowed = str(env["TELEGRAM_CHAT_ID"])
    state = _state()
    print(f"🤖 Бот-роутер запущен, слушаю чат {allowed}. Ctrl+C для остановки.")

    # В облаке (GitHub Actions) процесс живёт ограниченное время и сам перезапускается.
    # BOT_MAX_RUNTIME (сек) заставляет корректно выйти до лимита job'а; локально = 0 (бесконечно).
    start = time.time()
    try:
        max_runtime = int(env.get("BOT_MAX_RUNTIME") or 0)
    except (ValueError, TypeError):
        max_runtime = 0

    offset = _get_offset()
    while True:
        if max_runtime and (time.time() - start) > max_runtime:
            print("⏲ достигнут BOT_MAX_RUNTIME — выходим для перезапуска")
            return 0
        try:
            resp = requests.get(TG.format(token=token, method="getUpdates"),
                                params={"offset": offset + 1, "timeout": 50}, timeout=60)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"⚠️ getUpdates: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        if not data.get("ok"):
            # напр. 409 Conflict при кратковременной пересменке двух процессов — переждём
            print(f"⚠️ getUpdates не ok: {data.get('description')}", file=sys.stderr)
            time.sleep(3)
            continue

        for upd in data.get("result", []):
            offset = max(offset, upd["update_id"])
            OFFSET_FILE.write_text(str(offset))

            # нажатие inline-кнопки (✅/✏️/❌ под черновиком)
            if upd.get("callback_query"):
                _handle_callback(env, upd["callback_query"], allowed)
                continue

            msg = upd.get("message") or upd.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id"))
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            _REPLY_CHAT = chat_id  # отвечаем тому, кто написал
            _REPLY_KEYBOARD = None

            # /myid — узнать свой Telegram ID (доступно всем, чтобы выдать доступ)
            if text.lower().startswith("/myid"):
                _send(env, f"Твой Telegram ID: <code>{chat_id}</code>", keyboard=False)
                continue

            # ── доступ по человеку и набору режимов ──────────────────────────
            # Владелец (TELEGRAM_CHAT_ID) — все режимы. Команда — режимы из
            # team_access.json. Остальные — вежливый отказ.
            owner = (chat_id == allowed)
            modes = _allowed_modes(chat_id, allowed)
            if not owner and not modes:
                _send(env, "🔒 Это приватный бот <b>Welcs</b>. Доступа нет. "
                           "Если он тебе нужен — попроси владельца, и пришли ему свой ID "
                           "(узнать: отправь <code>/myid</code>).", keyboard=False)
                continue
            _REPLY_KEYBOARD = _keyboard_for(modes)  # показываем только разрешённые кнопки

            # ограниченному пользователю запрещаем выбирать чужой режим
            req = _mode_from_text(text)
            if req and req not in modes:
                _send(env, "🔒 У тебя нет доступа к этому режиму.")
                continue

            # выбор режима кнопкой
            if text == BTN_FF or text.lower() in ("fireflies", "/fireflies"):
                state[chat_id] = "fireflies"; _save_state(state)
                _send(env, "🔥 Режим: <b>Fireflies</b> (встречи). Напиши вопрос — найду в записях встреч.")
                continue
            if text == BTN_BX or text.lower() in ("bitrix", "/bitrix"):
                state[chat_id] = "bitrix"; _save_state(state)
                _send(env, "📊 Режим: <b>Bitrix</b> (CRM/задачи). Напиши вопрос — посмотрю в Bitrix24.")
                continue
            if text == BTN_GO or text.lower() in ("цели", "/goals", "/priorities"):
                state[chat_id] = "priorities"; _save_state(state)
                _send(env, "🎯 Режим: <b>Цели</b>. Скинь любую мысль, идею или задачу — разберу по твоим "
                           "целям, оценю приоритет, предложу кому делегировать и покажу черновик для записи.")
                continue
            if text == BTN_BD or text.lower() in ("совет", "/board", "/sovet"):
                state[chat_id] = "board"; _save_state(state)
                from board import render_menu
                cfgs = _board_cfg()
                menu_text, menu_kb = render_menu(cfgs.get(chat_id))
                _send_inline(env, menu_text, menu_kb)
                continue
            if text == BTN_MO or text.lower() in ("утро", "/morning", "/utro"):
                state[chat_id] = "morning"; _save_state(state)
                _send(env, "🌅 Режим: <b>Утро</b>. Напиши, что планируешь сегодня (списком) — прогоню "
                           "каждую задачу по твоим вопросам (хочу/важно/нужно, кайф, зачем на самом деле, "
                           "сам или делегировать) и сверю с целями на 3/6/12 мес.\n\n"
                           "Можешь и задать цели: напиши, например, «цель на 3 месяца: …».")
                continue
            if text == BTN_NO or text.lower() in ("notion", "/notion", "ноушн"):
                state[chat_id] = "notion"; _save_state(state)
                _send(env, "📝 Режим: <b>Notion</b>. Спроси про рабочее пространство: «какие страницы/базы "
                           "подключены?», «подключилась ли страница X?», «что в базе …?». Бот видит только то, "
                           "что расшарено его интеграции (••• → Connections).")
                continue
            if text.startswith("/start"):
                lines = {
                    "fireflies": "🔥 <b>Fireflies</b> — про встречи команды и собеседования.",
                    "bitrix": "📊 <b>Bitrix</b> — про CRM, сделки, задачи, чаты.",
                    "priorities": "🎯 <b>Цели</b> — кидай мысли/идеи/задачи, разберу по целям и запишу в Notion.",
                    "board": "🧠 <b>Совет</b> — стратегический вопрос → совет директоров из разных ИИ.",
                    "morning": "🌅 <b>Утро</b> — утренний прогон задач дня по твоим вопросам + связь с целями.",
                    "notion": "📝 <b>Notion</b> — что подключено в Notion, что в страницах/базах.",
                }
                avail = "\n".join(lines[m] for m in MODE_BTN if m in modes)
                _send(env, "Привет! Выбери режим кнопкой ниже, потом напиши:\n" + avail)
                continue

            src = state.get(chat_id)
            if not owner and src not in modes:  # снимаем чужой/устаревший режим
                src = None
            if not src and len(modes) == 1:     # один доступный режим → включаем сразу
                src = next(iter(modes)); state[chat_id] = src; _save_state(state)
            if not src:
                avail = " / ".join(MODE_BTN[m] for m in MODE_BTN if m in modes)
                _send(env, f"Сначала выбери режим кнопкой ниже 👇 ({avail}).")
                continue

            print(f"← [{src}] {text[:80]}")
            try:
                requests.post(TG.format(token=token, method="sendChatAction"),
                              json={"chat_id": chat_id, "action": "typing"}, timeout=10)
            except requests.RequestException:
                pass  # индикатор «печатает» не критичен

            # режим приоритизации: строим черновик и показываем кнопки подтверждения
            if src == "priorities":
                if not env.get("NOTION_API_KEY"):
                    _send(env, "⚠️ Режим 🎯 Цели не настроен: добавь <b>NOTION_API_KEY</b> в .env "
                               "и подключи интеграцию к базе «Инбокс».")
                    continue
                from priorities import build_draft, format_draft
                try:
                    res = build_draft(text, env)
                except Exception as e:  # noqa: BLE001
                    _send(env, f"⚠️ Ошибка при разборе: {e}")
                    continue
                if res.get("draft"):
                    pend = _pending(); pend[chat_id] = res["draft"]; _save_pending(pend)
                    _send_inline(env, "🎯 Вот черновик записи:\n\n" + format_draft(res["draft"]),
                                 DRAFT_KEYBOARD)
                else:
                    _send(env, "🎯 " + res.get("text", "—"))
                print("→ черновик отправлен")
                continue

            # режим совета директоров: опрос ролей + синтез
            if src == "board":
                from board import convene, format_board, effective_advisors
                advisors = effective_advisors(_board_cfg().get(chat_id))
                roster = ", ".join(f"{a['short']}·{a['provider']}" for a in advisors) or "пусто"
                _send(env, f"🧠 Собираю совет ({roster})… параллельно, ~20–40 сек.", keyboard=False)
                try:
                    result = convene(text, env, advisors=advisors)
                except Exception as e:  # noqa: BLE001
                    _send(env, f"⚠️ Совет не собрался: {e}")
                    continue
                _send(env, format_board(text, result))
                if env.get("NOTION_API_KEY"):
                    pend = _pending(); pend[chat_id + ":board"] = result["decision"]; _save_pending(pend)
                    _send_inline(env, "Записать это решение в Notion?", BOARD_KEYBOARD)
                print("→ совет отправлен")
                continue

            # режим утреннего прогона дня
            if src == "morning":
                if not env.get("NOTION_API_KEY"):
                    _send(env, "⚠️ Режим 🌅 Утро не настроен: нужен <b>NOTION_API_KEY</b> (цели хранятся в Notion).")
                    continue
                _send(env, "🌅 Прогоняю день по твоим вопросам… ~20–30 сек.", keyboard=False)
                from morning import morning_review
                try:
                    reply = morning_review(text, env)
                except Exception as e:  # noqa: BLE001
                    reply = f"⚠️ Ошибка прогона: {e}"
                _send(env, reply)
                print("→ прогон отправлен")
                continue

            # режим Notion: поиск/чтение рабочего пространства
            if src == "notion":
                _send(env, "📝 Смотрю в Notion… ~15–30 сек.", keyboard=False)
                try:
                    reply = answer_notion(text, env)
                except Exception as e:  # noqa: BLE001
                    reply = f"⚠️ Ошибка при обработке: {e}"
                _send(env, "📝 " + reply)
                print("→ ответ Notion отправлен")
                continue

            # режимы вопрос-ответ (Fireflies / Bitrix)
            _send(env, ("🔥 Ищу в Fireflies" if src == "fireflies" else "📊 Смотрю в Bitrix")
                       + "… это займёт ~20–30 сек, подожди.", keyboard=False)
            try:
                reply = answer_fireflies(text, env) if src == "fireflies" else answer_bitrix(text, env)
            except Exception as e:  # noqa: BLE001
                reply = f"⚠️ Ошибка при обработке: {e}"
            prefix = "🔥 " if src == "fireflies" else "📊 "
            _send(env, prefix + reply)
            print("→ ответ отправлен")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
