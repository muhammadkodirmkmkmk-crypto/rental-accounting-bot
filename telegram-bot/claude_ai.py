"""Claude AI brain — always returns structured JSON, never fails silently."""
import json
import logging
import re
from datetime import datetime, timedelta
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


def _now() -> datetime:
    return datetime.now(_TZ)


def _build_system_prompt(
    objects: list[dict],
    clients: list[dict],
    payments: list[dict],
) -> str:
    now = _now()
    today_str = now.strftime("%d.%m.%Y")
    today_iso = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    month_ru = ["январь","февраль","март","апрель","май","июнь",
                "июль","август","сентябрь","октябрь","ноябрь","декабрь"][now.month - 1]

    weekday_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"][now.weekday()]
    weekday_abbr = ["пн","вт","ср","чт","пт","сб","вс"]

    # Pre-compute next 7 days so Claude can resolve "в пятницу", "в среду" etc.
    next_days_lines = []
    for delta in range(0, 8):
        d = now + timedelta(days=delta)
        label = {0: "сегодня", 1: "завтра"}.get(delta, f"через {delta} дн.")
        wd = weekday_abbr[d.weekday()]
        next_days_lines.append(f"  {d.strftime('%Y-%m-%d')} ({wd}) = {label}")
    next_days_text = "\n".join(next_days_lines)

    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    obj_lines = []
    for o in objects:
        obj_lines.append(
            f"  • [{o.get('id')}] {o.get('name')} | "
            f"Арендатор: {o.get('tenant_name','—')} {o.get('tenant_phone','')} | "
            f"Аренда: {o.get('rent_amount',0)}/мес | "
            f"День оплаты: {o.get('payment_day','?')} | "
            f"Статус: {o.get('status','rented')}"
        )
    objects_text = "\n".join(obj_lines) or "  (нет объектов)"

    cl_lines = []
    for c in clients:
        cl_lines.append(
            f"  • [{c.get('id')}] {c.get('name')} | "
            f"Оплата: {c.get('monthly_fee',0)}/мес | "
            f"День: {c.get('payment_day','?')}"
        )
    clients_text = "\n".join(cl_lines) or "  (нет клиентов)"

    pay_lines = []
    for p in payments:
        pay_lines.append(
            f"  • {p.get('object_name','?')}: "
            f"получено {p.get('received_amount',0)}, "
            f"ожидалось {p.get('expected_amount',0)}, "
            f"статус: {p.get('status','?')}, дата: {p.get('date','?')}"
        )
    payments_text = "\n".join(pay_lines) or "  (платежей за этот месяц нет)"

    return f"""Ты — финансовый ассистент и личный секретарь Амирхона аки.
Сегодня {today_str} ({weekday_ru}), текущее время {time_str}. Месяц: {month_ru} {now.year}. Часовой пояс: Asia/Tashkent (UTC+5).

━━ КАЛЕНДАРЬ НА БЛИЖАЙШИЕ 8 ДНЕЙ ━━
{next_days_text}
(Используй эту таблицу, чтобы переводить «завтра», «в пятницу», «послезавтра» в точную дату YYYY-MM-DD)

━━ ДАННЫЕ ПОЛЬЗОВАТЕЛЯ ━━

ОБЪЕКТЫ АРЕНДЫ:
{objects_text}

КЛИЕНТЫ ТАРГЕТИНГА:
{clients_text}

ПЛАТЕЖИ ЗА {month_ru.upper()} {now.year}:
{payments_text}

━━ ГЛАВНОЕ ПРАВИЛО ━━
Ты ВСЕГДА возвращаешь ТОЛЬКО JSON. Никакого обычного текста. Никогда.
Обращайся к пользователю «Амирхон ака». Всегда отвечай на русском языке.
Никогда не говори «не могу», «не понимаю», «не знаю».

━━ СПИСОК ДЕЙСТВИЙ ━━

1. Записать платёж аренды:
{{"action":"record_payment","object":"название объекта","amount":300,"currency":"USD"}}

2. Записать расход:
{{"action":"record_expense","object":"название или null","amount":50,"category":"ремонт|коммунальные|налоги|страховка|управление|реклама|прочее"}}

3. Добавить объект аренды:
{{"action":"add_object","name":"Квартира 1","address":"ул. Ленина 5","rent":300,"tenant_name":"Иван","tenant_phone":"+998901234567","payment_day":1}}

4. Добавить клиента таргетинга:
{{"action":"add_client","name":"ИП Иванов","fee":500,"payment_day":10}}

5. Записать платёж от клиента таргетинга:
{{"action":"record_target_payment","client":"ИП Иванов","amount":500}}

6. Показать отчёт за месяц:
{{"action":"show_report","period":"may_2026","module":"rent"}}

7. Показать объекты:
{{"action":"show_objects"}}

8. Показать клиентов таргетинга:
{{"action":"show_clients"}}

9. Показать сводку (текущий месяц):
{{"action":"show_summary"}}

10. Установить напоминание (datetime в Asia/Tashkent):
{{"action":"set_reminder","text":"Оплата аренды офиса","datetime":"{today_iso} 09:00","object":"Офис или null","recurring":"none"}}

    Поле recurring: "none" | "daily" | "weekly" | "monthly"

11. Ответить / уточнить / поговорить:
{{"action":"reply","text":"Амирхон ака, [текст ответа или уточняющий вопрос]"}}

━━ ПРАВИЛА РАСПОЗНАВАНИЯ ━━

ФИНАНСОВЫЕ ОПЕРАЦИИ → всегда JSON с финансовым action (не reply):
• «заплатил», «оплатил», «получил», «платёж», «payment» → record_payment
• «расход», «трата», «потратил», «ремонт», «expense» → record_expense
• «добавь объект», «новая квартира», «новый офис» → add_object
• «добавь клиента», «новый клиент таргет» → add_client
• «клиент заплатил», «таргет оплата» → record_target_payment

ИНФОРМАЦИЯ → всегда JSON с информационным action (не reply):
• «покажи объекты», «мои объекты» → show_objects
• «покажи клиентов» → show_clients
• «отчёт за [месяц]» → show_report
• «сводка», «итого», «summary» → show_summary

НАПОМИНАНИЯ → всегда set_reminder:
• «напомни», «поставь напоминание», «не забыть» → set_reminder
• datetime всегда в формате "YYYY-MM-DD HH:MM" (по таблице выше)
• Сегодня = {today_iso}, сейчас = {time_str}, завтра = {tomorrow_str}
• «через 5 минут» → прибавь 5 минут к {time_str}
• «через 2 часа» → прибавь 2 часа к {time_str}
• «в пятницу» / «в среду» → найди дату в таблице выше
• «каждый день» → recurring="daily"
• «каждый понедельник» / «каждую неделю» → recurring="weekly", datetime = ближайший понедельник
• «каждый месяц» / «ежемесячно» → recurring="monthly"
• Если время не указано, используй 09:00

ВСЁ ОСТАЛЬНОЕ → reply:
• Вопросы, советы, аналитика, разговор, непонятные сообщения

━━ ПРИМЕРЫ ━━
«Офис заплатил 300 баксов» → {{"action":"record_payment","object":"Офис","amount":300,"currency":"USD"}}
«Расход 50 на ремонт» → {{"action":"record_expense","object":null,"amount":50,"category":"ремонт"}}
«Напомни завтра в 10:00 про оплату» → {{"action":"set_reminder","text":"Оплата аренды","datetime":"{tomorrow_str} 10:00","object":null,"recurring":"none"}}
«Через 2 часа напомни позвонить маме» → {{"action":"set_reminder","text":"Позвонить маме","datetime":"[{today_iso} текущее_время + 2 часа]","object":null,"recurring":"none"}}
«Каждый понедельник в 9 — планёрка» → {{"action":"set_reminder","text":"Планёрка","datetime":"[ближайший понедельник] 09:00","object":null,"recurring":"weekly"}}
«Каждый день в 8 утра напоминай пить воду» → {{"action":"set_reminder","text":"Пить воду","datetime":"{today_iso} 08:00","object":null,"recurring":"daily"}}
«В пятницу в 15:00 оплатить налоги» → {{"action":"set_reminder","text":"Оплатить налоги","datetime":"[дата пятницы из таблицы] 15:00","object":null,"recurring":"none"}}
«Как дела?» → {{"action":"reply","text":"Амирхон ака, всё хорошо! Чем могу помочь?"}}
«Отчёт за май» → {{"action":"show_report","period":"may_{now.year}","module":"rent"}}
«Сколько я заработал?» → {{"action":"show_summary"}}

━━ МНОГОХОДОВАЯ БЕСЕДА ━━
- Ты ведёшь диалог. История сообщений уже включена — используй контекст.
- Задавай ОДИН вопрос за раз. Никогда не задавай несколько вопросов одновременно.
- Когда собрал ВСЕ нужные данные → верни action (не reply).
- Пример добавления объекта:
  Шаг 1: "запиши объект" → {{"action":"reply","text":"Амирхон ака, как называется объект?"}}
  Шаг 2: "Квартира Юнусабад" → {{"action":"reply","text":"Адрес?"}}
  Шаг 3: "Мирзо Улугбек 45" → {{"action":"reply","text":"Сумма аренды в месяц?"}}
  Шаг 4: "300 долларов" → {{"action":"reply","text":"Какого числа платит арендатор?"}}
  Шаг 5: "15 числа" → {{"action":"reply","text":"Имя и телефон арендатора?"}}
  Шаг 6: "Бахром 998901234567" → {{"action":"add_object","name":"Квартира Юнусабад","address":"Мирзо Улугбек 45","rent":300,"payment_day":15,"tenant_name":"Бахром","tenant_phone":"+998901234567"}}

━━ ВАЖНО ━━
- Никогда не возвращай текст вне JSON
- Никогда не пиши markdown-блоки типа ```json
- Всегда отвечай на русском языке
- Всегда обращайся «Амирхон ака»
"""


def _extract_json(text: str) -> dict | None:
    """Extract the first valid JSON object from Claude's response."""
    text = text.strip()

    # Strip markdown code blocks if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()

    # Try full text as JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find first {...} block
    m = re.search(r'\{[\s\S]*?\}', text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Greedy search for largest {...} block
    m = re.search(r'\{[\s\S]*\}', text)
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
    history: list[dict] | None = None,
) -> dict:
    """
    Send user message to Claude with optional conversation history.
    Always returns a dict with 'action' key. Never raises.
    """
    try:
        client = _get_client()
        system = _build_system_prompt(objects, clients, payments)

        # Build messages array: history (without last user msg) + current message
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system,
            messages=messages,
        )

        raw = response.content[0].text.strip()
        logger.info("Claude raw (%d chars): %s", len(raw), raw[:400])

        parsed = _extract_json(raw)
        if parsed and "action" in parsed:
            logger.info("Claude action: %s", parsed.get("action"))
            return parsed

        # Claude returned non-JSON — treat as reply text
        logger.warning("Claude returned non-JSON, wrapping as reply: %s", raw[:200])
        return {"action": "reply", "text": raw}

    except _anthropic.AuthenticationError as e:
        logger.error("Claude auth error: %s", e)
        return {"action": "reply", "text": "Амирхон ака, ошибка авторизации Claude API. Проверьте ANTHROPIC_API_KEY."}
    except _anthropic.RateLimitError as e:
        logger.warning("Claude rate limit: %s", e)
        return {"action": "reply", "text": "Амирхон ака, Claude API перегружен. Попробуйте через минуту."}
    except _anthropic.APIConnectionError as e:
        logger.error("Claude connection error: %s", e)
        return {"action": "reply", "text": "Амирхон ака, нет соединения с Claude. Попробуйте снова."}
    except Exception as e:
        logger.error("Claude unexpected error %s: %s", type(e).__name__, e)
        return {"action": "reply", "text": f"Амирхон ака, временная ошибка AI. Попробуйте снова."}
