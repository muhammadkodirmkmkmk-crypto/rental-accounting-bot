"""Claude AI brain for the rental accounting bot."""
import json
import logging
import re
from datetime import datetime
import pytz
import anthropic as _anthropic

import config

logger = logging.getLogger(__name__)

_TZ = pytz.timezone("Asia/Tashkent")
_client: _anthropic.Anthropic | None = None


def _get_client() -> _anthropic.Anthropic:
    global _client
    if _client is None:
        _client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _today_str() -> str:
    return datetime.now(_TZ).strftime("%d.%m.%Y")


def _month_name_ru() -> str:
    return ["январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"][datetime.now(_TZ).month - 1]


def _build_system_prompt(
    objects: list[dict],
    clients: list[dict],
    payments: list[dict],
) -> str:
    today = _today_str()
    month = _month_name_ru()
    now = datetime.now(_TZ)

    obj_lines = []
    for o in objects:
        obj_lines.append(
            f"  • ID={o.get('id')} | {o.get('name')} | "
            f"Арендатор: {o.get('tenant_name', '—')} | "
            f"Тел: {o.get('tenant_phone', '—')} | "
            f"Аренда: {o.get('rent_amount', 0)}/мес | "
            f"День оплаты: {o.get('payment_day', '?')} | "
            f"Статус: {o.get('status', 'rented')}"
        )
    objects_text = "\n".join(obj_lines) if obj_lines else "  (объектов нет)"

    cl_lines = []
    for c in clients:
        cl_lines.append(
            f"  • ID={c.get('id')} | {c.get('name')} | "
            f"Оплата: {c.get('monthly_fee', 0)}/мес | "
            f"День: {c.get('payment_day', '?')}"
        )
    clients_text = "\n".join(cl_lines) if cl_lines else "  (клиентов нет)"

    pay_lines = []
    for p in payments:
        pay_lines.append(
            f"  • {p.get('object_name', '?')}: получено {p.get('received_amount', 0)}, "
            f"ожидалось {p.get('expected_amount', 0)}, "
            f"статус: {p.get('status', '?')}, дата: {p.get('date', '?')}"
        )
    payments_text = "\n".join(pay_lines) if pay_lines else "  (платежей за этот месяц нет)"

    return f"""Ты — умный финансовый ассистент и правая рука Амирхона аки.
Сегодня {today} ({month} {now.year}). Часовой пояс: Asia/Tashkent (UTC+5).

━━ ОБЪЕКТЫ АРЕНДЫ ━━
{objects_text}

━━ КЛИЕНТЫ ТАРГЕТИНГА ━━
{clients_text}

━━ ПЛАТЕЖИ ЗА ТЕКУЩИЙ МЕСЯЦ ({month.upper()}) ━━
{payments_text}

━━ ПРАВИЛА ━━
1. Всегда отвечай на РУССКОМ языке. Обращайся к пользователю "Амирхон ака".
2. Понимай сообщения на русском, узбекском и английском.
3. Будь краток и дружелюбен. Используй эмодзи умеренно.
4. Если пользователь хочет выполнить ДЕЙСТВИЕ — верни ТОЛЬКО валидный JSON (без markdown, без пояснений).
5. Если анализируешь или отвечаешь на вопрос — верни обычный текст.

━━ JSON-ДЕЙСТВИЯ (только JSON, без других слов) ━━
Записать платёж аренды:
{{"action":"record_payment","object_name":"точное название объекта","amount":300}}

Записать расход:
{{"action":"record_expense","object_name":"название или null","category":"repair|utilities|tax|insurance|management|advertising|other","amount":50}}

Отчёт за месяц:
{{"action":"get_report","month":5,"year":2025}}

Сводка за текущий месяц:
{{"action":"get_summary"}}

Список объектов:
{{"action":"list_objects"}}

Список арендаторов:
{{"action":"list_tenants"}}

Установить напоминание (дата в формате YYYY-MM-DD, время HH:MM в Asia/Tashkent):
{{"action":"set_reminder","object_name":"Офис или null","date":"{now.strftime('%Y-%m-%d')}","time":"09:00","message":"Оплата аренды"}}

━━ ПРИМЕРЫ РАСПОЗНАВАНИЯ ━━
"Квартира Чиланзар заплатила 300 баксов" → {{"action":"record_payment","object_name":"Квартира Чиланзар","amount":300}}
"Покажи сколько заработал в мае" → {{"action":"get_report","month":5,"year":{now.year}}}
"Добавь расход 50 на ремонт в офисе" → {{"action":"record_expense","object_name":"офис","category":"repair","amount":50}}
"Как дела с платежами?" → аналитический ответ текстом (смотри данные выше и дай оценку)
"Сводка" → {{"action":"get_summary"}}
"Покажи арендаторов" → {{"action":"list_tenants"}}
"Напомни завтра в 10:00 про оплату офиса" → {{"action":"set_reminder","object_name":"офис","date":"{(datetime.now(_TZ).date() + __import__('datetime').timedelta(days=1)).strftime('%Y-%m-%d')}","time":"10:00","message":"Оплата аренды офиса"}}
"Поставь напоминание на 25 мая в 13:30" → {{"action":"set_reminder","object_name":null,"date":"{now.year}-05-25","time":"13:30","message":"Напоминание"}}

━━ ВАЖНО ПРО НАПОМИНАНИЯ ━━
Бот УМЕЕТ устанавливать напоминания через APScheduler. Когда пользователь просит напомнить —
ВСЕГДА возвращай JSON с action="set_reminder". Никогда не говори что не можешь установить напоминание.
Если дата/время не указаны явно — уточни у пользователя.
"""


def _extract_json(text: str) -> dict | None:
    """Extract the first JSON object from Claude's response."""
    text = text.strip()
    # Try full text as JSON first
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    # Try to find a JSON block inside text (e.g. wrapped in markdown)
    m = re.search(r'\{[\s\S]*?\}', text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def process_message(
    user_message: str,
    objects: list[dict],
    clients: list[dict],
    payments: list[dict],
) -> dict:
    """
    Send user message to Claude with full rental context.

    Returns one of:
      {"type": "action", "action": "record_payment", ...}   # structured action
      {"type": "text",   "content": "..."}                  # plain text reply
      {"type": "error",  "content": "..."}                  # API/network error
    """
    try:
        client = _get_client()
        system = _build_system_prompt(objects, clients, payments)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        logger.info("Claude raw response (%d chars): %s", len(raw), raw[:300])

        parsed = _extract_json(raw)
        if parsed and "action" in parsed:
            logger.info("Claude action detected: %s", parsed.get("action"))
            return {"type": "action", **parsed}

        return {"type": "text", "content": raw}

    except _anthropic.AuthenticationError as e:
        logger.error("Claude auth error: %s", e)
        return {"type": "error", "content": "ошибка авторизации Claude API"}
    except _anthropic.RateLimitError as e:
        logger.warning("Claude rate limit: %s", e)
        return {"type": "error", "content": "Claude API перегружен, попробуйте через минуту"}
    except _anthropic.APIConnectionError as e:
        logger.error("Claude connection error: %s", e)
        return {"type": "error", "content": "нет соединения с Claude API"}
    except Exception as e:
        logger.error("Claude unexpected error: %s: %s", type(e).__name__, e)
        return {"type": "error", "content": f"ошибка AI ({type(e).__name__})"}
