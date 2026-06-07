#!/usr/bin/env python3
"""
🧠 Совет директоров — режим бота @AuditorWelcs_bot.

Руководитель задаёт стратегический вопрос → собирается общий бриф по бизнесу
из ВСЕХ источников параллельно (OKR + Notion: цели недели и Инбокс + Bitrix24:
просрочки и сделки + Fireflies: встречи за ~45 дней) → совет «директоров» (роль +
своя модель) отвечает ПАРАЛЛЕЛЬНО → председатель (Claude) сводит в одно решение.

Разные модели = реальное разнообразие мышления, а не один ИИ в пяти масках.
Если ключа GPT/Gemini нет — роль автоматически играет Claude.
"""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor

import anthropic

from model_clients import ask
from priorities import OKR, _tool_get_inbox, _tool_read_goals

BUSINESS = ("Welcs — управление краткосрочной арендой недвижимости на Costa Brava "
            "(Испания): уборки, заезды/выезды, поддержка гостей и собственников, "
            "маркетинг и бронирования, миграция учёта в Xero, разработка своей платформы "
            "и AI-агентов. Руководитель — Михаил.")

# Состав совета: роль + провайдер по умолчанию + фокус
ADVISORS = [
    {"key": "cfo", "emoji": "💰", "short": "CFO", "title": "CFO — финансовый директор", "provider": "openai",
     "focus": "финансы, экономика юнита, кэшфлоу, окупаемость, риски затрат, миграция учёта в Xero. "
              "Считай деньги и сроки окупаемости, называй цифры и допущения."},
    {"key": "cmo", "emoji": "📣", "short": "CMO", "title": "CMO — директор по маркетингу", "provider": "gemini",
     "focus": "привлечение гостей, бронирования, бренд, SMM, реклама, каналы (Booking/Airbnb/прямые), "
              "конверсия и стоимость привлечения. Мысли ростом выручки и спросом."},
    {"key": "coo", "emoji": "⚙️", "short": "COO", "title": "COO — операционный директор", "provider": "claude",
     "focus": "операции: уборки, заезды, инциденты, качество сервиса, нагрузка и процессы команды, SLA. "
              "Думай про исполнимость, узкие места и масштабирование без потери качества."},
    {"key": "cto", "emoji": "🧩", "short": "CTO", "title": "CTO / Product", "provider": "claude",
     "focus": "платформа, AI-агенты, автоматизации, данные, технический долг и приоритеты разработки. "
              "Оценивай, что автоматизировать, а что рано."},
    {"key": "chro", "emoji": "👥", "short": "CHRO", "title": "CHRO — директор по персоналу", "provider": "openai",
     "focus": "команда: найм, роли, мотивация, удержание, структура и делегирование. "
              "Думай про людей, нагрузку и кто что тянет."},
    {"key": "revenue", "emoji": "📈", "short": "Revenue", "title": "Revenue-директор", "provider": "gemini",
     "focus": "доходность объектов: ценообразование и динамика тарифов, загрузка (occupancy), ADR/RevPAR, "
              "сезонность, длительность бронирований, маржинальность каналов (Booking/Airbnb/прямые) и "
              "прогноз выручки. Думай про максимизацию дохода с портфеля апартаментов."},
]

ROLE_BY_KEY = {a["key"]: a for a in ADVISORS}

# Доступные модели для роли. tap по кнопке роли переключает по кругу.
PROVIDER_CYCLE = ["claude", "openai", "gemini", "off"]
PROVIDER_LABEL = {"claude": "Claude", "openai": "GPT", "gemini": "Gemini", "off": "⛔ выкл"}


def default_config() -> dict:
    """Состав по умолчанию: {role_key: provider}."""
    return {a["key"]: a["provider"] for a in ADVISORS}


def cycle_provider(current: str) -> str:
    """Следующая модель по кругу при тапе на кнопку роли."""
    try:
        i = PROVIDER_CYCLE.index(current)
    except ValueError:
        i = -1
    return PROVIDER_CYCLE[(i + 1) % len(PROVIDER_CYCLE)]


def effective_advisors(config: dict | None) -> list[dict]:
    """Активные советники с учётом выбранных моделей (off — исключаются)."""
    cfg = config or default_config()
    out = []
    for a in ADVISORS:
        prov = cfg.get(a["key"], a["provider"])
        if prov and prov != "off":
            out.append({**a, "provider": prov})
    return out


def _iso(days_ago: int) -> str:
    d = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _notion_section(env: dict) -> str:
    """Цели недели + текущий Инбокс из Notion."""
    if not env.get("NOTION_API_KEY"):
        return ""
    out = []
    goals = _tool_read_goals(env)
    if goals and not goals.startswith("("):
        out.append(f"ЦЕЛИ (из Инбокса, Тип=Цель):\n{goals[:1500]}")
    try:
        data = json.loads(_tool_get_inbox(env, True))
        if data.get("items"):
            out.append(f"ТЕКУЩИЙ ИНБОКС ({data['count']} открытых):\n" +
                       json.dumps(data["items"][:25], ensure_ascii=False))
    except (ValueError, TypeError):
        pass
    return "\n\n".join(out)


def _bitrix_section(env: dict) -> str:
    """Состояние Bitrix24: просроченные задачи + открытые сделки."""
    if not env.get("BITRIX_WEBHOOK_URL"):
        return ""
    try:
        from bitrix_client import Bitrix
        bx = Bitrix(env["BITRIX_WEBHOOK_URL"])
        now = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        overdue = bx.call_list(
            "tasks.task.list",
            {"filter": {"<DEADLINE": now, "@REAL_STATUS": [2, 3]},
             "select": ["ID", "TITLE", "DEADLINE", "RESPONSIBLE_ID"]}, max_items=30)
        deals = bx.call_list(
            "crm.deal.list",
            {"filter": {"CLOSED": "N"}, "select": ["ID", "TITLE", "OPPORTUNITY"]}, max_items=50)
        n_over = f"{len(overdue)}+" if len(overdue) >= 30 else str(len(overdue))
        lines = [f"Просроченных задач (выборка): {n_over}"]
        for t in overdue[:8]:
            title = t.get("title") or t.get("TITLE") or "?"
            dl = (t.get("deadline") or t.get("DEADLINE") or "")[:10]
            lines.append(f"  • {title} (до {dl})")
        n_deals = f"{len(deals)}+" if len(deals) >= 50 else str(len(deals))
        opp = sum(float(d.get("OPPORTUNITY") or 0) for d in deals)
        lines.append(f"Открытых сделок (выборка): {n_deals}, сумма по выборке ~{opp:.0f}")
        return "СОСТОЯНИЕ BITRIX24:\n" + "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"(Bitrix недоступен: {e})"


FF_DAYS = 45        # окно истории встреч для контекста совета
FF_MAX = 30         # сколько встреч максимум включать в бриф


def _fireflies_section(env: dict) -> str:
    """Сводка встреч команды за FF_DAYS дней из Fireflies (резюме + ключевые слова)."""
    if not env.get("FIREFLIES_API_KEY"):
        return ""
    try:
        from fireflies_client import Fireflies
        items = Fireflies(env["FIREFLIES_API_KEY"]).list_transcripts(_iso(FF_DAYS), _iso(0), limit=50)
        if not items:
            return ""
        lines = []
        for t in items[:FF_MAX]:
            s = t.get("summary") or {}
            summ = (s.get("short_summary") or s.get("overview") or "").replace("\n", " ")[:280]
            kw = s.get("keywords") or []
            kw_str = (" [" + ", ".join(kw[:6]) + "]") if kw else ""
            lines.append(f"  • {t.get('title')} ({(t.get('dateString') or '')[:10]}): {summ}{kw_str}")
        shown = min(len(items), FF_MAX)
        more = f" (показано {shown} из {len(items)})" if len(items) > FF_MAX else ""
        return f"ВСТРЕЧИ ЗА {FF_DAYS} ДНЕЙ — {len(items)} шт{more}:\n" + "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"(Fireflies недоступен: {e})"


def build_context(env: dict) -> str:
    """Общий бриф по бизнесу из всех источников (Notion + Bitrix + Fireflies),
    собирается ПАРАЛЛЕЛЬНО. Видит каждый советник."""
    parts = [f"БИЗНЕС: {BUSINESS}", f"ЦЕЛИ КВАРТАЛА (OKR):\n{OKR}"]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fn, env) for fn in
                   (_notion_section, _bitrix_section, _fireflies_section)]
        for f in futures:
            sec = f.result()
            if sec:
                parts.append(sec)
    return "\n\n".join(parts)


def _advisor_system(adv: dict, context: str) -> str:
    return (f"Ты — {adv['title']} в совете директоров компании. Твоя зона: {adv['focus']}\n\n"
            f"КОНТЕКСТ БИЗНЕСА (общий для всего совета):\n{context}\n\n"
            "Дай мнение СТРОГО со своей позиции. Будь конкретным: рекомендация, 1–2 ключевых риска "
            "и что сделать первым шагом. Опирайся на контекст и цели. Максимум ~150 слов, по-русски. "
            "Без воды и общих фраз.")


def _ask_advisor(adv: dict, question: str, context: str, env: dict) -> dict:
    system = _advisor_system(adv, context)
    try:
        text, used = ask(adv["provider"], system, question, env, max_tokens=600)
    except Exception as e:  # noqa: BLE001
        text, used = f"(не удалось получить мнение: {e})", "—"
    return {**adv, "text": text, "used": used}


CHAIR_TOOL = [{
    "name": "board_decision",
    "description": "Итоговое решение председателя совета. Вызови один раз.",
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "description": "Короткий заголовок решения/вопроса."},
        "objective": {"type": "string",
                      "enum": ["O1 Платформа", "O2 AI-агенты", "O3 Задачи и автоматизации",
                               "O4 Xero", "O5 Личный рост", "Без цели"]},
        "recommendation": {"type": "string", "description": "Чёткая рекомендация совета (2–4 предложения)."},
        "next_step": {"type": "string", "description": "Конкретный первый шаг."},
        "disagreement": {"type": "string", "description": "Где директора разошлись (если разошлись)."}},
        "required": ["title", "objective", "recommendation", "next_step"]}}]


def _synthesize(question: str, opinions: list[dict], context: str, env: dict) -> dict:
    client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])
    model = env.get("MODEL", "claude-opus-4-8")
    board_text = "\n\n".join(f"{o['emoji']} {o['title']} ({o['used']}):\n{o['text']}" for o in opinions)
    system = ("Ты — председатель совета директоров (CEO). Тебе даны вопрос руководителя и мнения "
              f"директоров. КОНТЕКСТ:\n{context}\n\nВзвесь мнения, разреши противоречия и прими решение "
              "в интересах бизнеса. Вызови board_decision.")
    user = f"ВОПРОС РУКОВОДИТЕЛЯ:\n{question}\n\nМНЕНИЯ СОВЕТА:\n{board_text}"
    resp = client.messages.create(
        model=model, max_tokens=1500,
        system=[{"type": "text", "text": system}],
        tools=CHAIR_TOOL, tool_choice={"type": "tool", "name": "board_decision"},
        messages=[{"role": "user", "content": user}])
    for b in resp.content:
        if b.type == "tool_use" and b.name == "board_decision":
            return dict(b.input)
    return {"title": question[:80], "objective": "Без цели",
            "recommendation": "(не удалось свести мнения)", "next_step": ""}


def convene(question: str, env: dict, advisors: list[dict] | None = None) -> dict:
    """Собирает совет выбранным составом: возвращает {opinions, decision}."""
    board = advisors if advisors is not None else ADVISORS
    if not board:
        return {"opinions": [], "decision": {
            "title": question[:80], "objective": "Без цели",
            "recommendation": "Совет пуст — включи хотя бы одну роль в настройках (🧠 Совет).",
            "next_step": ""}}
    context = build_context(env)
    with ThreadPoolExecutor(max_workers=len(board)) as ex:
        opinions = list(ex.map(lambda a: _ask_advisor(a, question, context, env), board))
    decision = _synthesize(question, opinions, context, env)
    return {"opinions": opinions, "decision": decision}


def render_menu(config: dict | None) -> tuple[str, dict]:
    """Меню настройки совета: текст + inline-клавиатура.
    Тап по роли переключает модель (Claude→GPT→Gemini→⛔), кнопки Сброс/Готово."""
    cfg = config or default_config()
    active = sum(1 for a in ADVISORS if cfg.get(a["key"], a["provider"]) != "off")
    text = ("🧠 <b>Совет директоров — состав</b>\n"
            "Жми на роль, чтобы сменить ИИ-модель (Claude → GPT → Gemini → выкл).\n"
            f"Активно ролей: <b>{active}</b>. Когда готово — «✅ Готово» и задавай вопрос.")
    rows = []
    for a in ADVISORS:
        prov = cfg.get(a["key"], a["provider"])
        label = f"{a['emoji']} {a['short']} → {PROVIDER_LABEL.get(prov, prov)}"
        rows.append([{"text": label, "callback_data": f"cfg:{a['key']}"}])
    rows.append([{"text": "♻️ Сброс", "callback_data": "cfg:reset"},
                 {"text": "✅ Готово", "callback_data": "cfg:done"}])
    return text, {"inline_keyboard": rows}


def format_board(question: str, result: dict) -> str:
    lines = [f"🧠 <b>Совет директоров</b> по вопросу:", f"<i>{question[:300]}</i>", ""]
    for o in result["opinions"]:
        lines.append(f"{o['emoji']} <b>{o['title']}</b> · <i>{o['used']}</i>")
        lines.append(o["text"])
        lines.append("")
    d = result["decision"]
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"🎯 <b>Решение председателя</b> (цель: {d.get('objective', '—')})")
    lines.append(d.get("recommendation", ""))
    if d.get("next_step"):
        lines.append(f"\n👉 <b>Первый шаг:</b> {d['next_step']}")
    if d.get("disagreement"):
        lines.append(f"\n⚖️ <i>Разногласия: {d['disagreement']}</i>")
    return "\n".join(lines)
