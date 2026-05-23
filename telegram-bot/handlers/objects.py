import asyncio
import logging
import re
from datetime import datetime, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import sheets
import analytics
from database import get_state, set_state, clear_state, get_user_settings
from handlers.start import main_menu_keyboard

logger = logging.getLogger(__name__)

OBJECT_FIELDS = [
    ("add_object_name",    "🏠 *Название объекта* (например: Квартира 1, Офис на Ленина):"),
    ("add_object_address", "📍 *Адрес объекта*:"),
    ("add_object_tenant",  "👤 *ФИО арендатора*:"),
    ("add_object_phone",   "📞 *Телефон арендатора*:"),
    ("add_object_rent",    "💰 *Стандартная сумма аренды в месяц* (цифрами, например: 350):"),
    ("add_object_day",     "📅 *День оплаты каждого месяца* (1-28):"),
    ("add_object_start",   "📅 *Дата начала аренды* (ДД.ММ.ГГГГ, например: 01.06.2025):"),
    ("add_object_end",     "📅 *Дата окончания аренды* (ДД.ММ.ГГГГ):"),
]

DISCOUNT_FIELDS = [
    ("add_object_initial_price", "💵 *Начальная (скидочная) цена* (цифрами):"),
    ("add_object_discount_end",  "📅 *Конец скидочного периода* (ДД.ММ.ГГГГ или количество месяцев, например: 3):"),
]

ALL_TEXT_STATES = {f[0] for f in OBJECT_FIELDS} | {f[0] for f in DISCOUNT_FIELDS}
FIELD_KEYS = [
    "name", "address", "tenant_name", "tenant_phone",
    "rent_amount", "payment_day", "lease_start", "lease_end",
]


def _parse_amount(text: str) -> float:
    cleaned = re.sub(r"[^\d.,]", "", text.replace(" ", ""))
    if not cleaned:
        raise ValueError(f"No numeric content in: {text!r}")
    cleaned = cleaned.replace(",", ".")
    if cleaned.count(".") > 1:
        parts = cleaned.rsplit(".", 1)
        cleaned = parts[0].replace(".", "") + "." + parts[1]
    return float(cleaned)


def get_current_rent(obj: dict) -> float:
    """Return the rent amount that applies today (initial price if within discount period)."""
    regular = float(obj.get("rent_amount", 0) or 0)
    initial = obj.get("initial_price", "")
    discount_end = obj.get("discount_end", "")
    if initial and discount_end:
        try:
            end_dt = datetime.strptime(str(discount_end), "%d.%m.%Y").date()
            if date.today() <= end_dt:
                return float(initial)
        except ValueError:
            pass
    return regular


async def add_object_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_state(user_id)
    set_state(user_id, "add_object_name", {})
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(
            "🏠 Добавляем новый объект!\n\n" + OBJECT_FIELDS[0][1],
            parse_mode="Markdown",
        )


async def start_add_object_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await add_object_command(update, context)


async def add_object_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)

    if state not in ALL_TEXT_STATES:
        return

    value = update.message.text.strip()

    # ── Discount flow states ──────────────────────────────────
    if state == "add_object_initial_price":
        try:
            initial = _parse_amount(value)
        except ValueError:
            await update.message.reply_text("Введите сумму цифрами, например: 250")
            return
        data["initial_price"] = str(initial)
        set_state(user_id, "add_object_discount_end", data)
        await update.message.reply_text(
            DISCOUNT_FIELDS[1][1] + "\n\n_Введите дату (ДД.ММ.ГГГГ) или число месяцев (например: 3)_",
            parse_mode="Markdown",
        )
        return

    if state == "add_object_discount_end":
        # Accept either DD.MM.YYYY or N months
        discount_end_val = ""
        if re.match(r"^\d+$", value):
            months = int(value)
            from dateutil.relativedelta import relativedelta  # type: ignore[import]
            lease_start_str = data.get("lease_start", "")
            try:
                start_dt = datetime.strptime(lease_start_str, "%d.%m.%Y")
                end_dt = start_dt + relativedelta(months=months)
                discount_end_val = end_dt.strftime("%d.%m.%Y")
            except Exception:
                today = date.today()
                from dateutil.relativedelta import relativedelta as rd
                discount_end_val = (today + rd(months=months)).strftime("%d.%m.%Y")
        else:
            try:
                datetime.strptime(value, "%d.%m.%Y")
                discount_end_val = value
            except ValueError:
                await update.message.reply_text(
                    "Введите дату в формате ДД.ММ.ГГГГ (например: 01.09.2025) или число месяцев (например: 3)."
                )
                return
        data["discount_end"] = discount_end_val
        set_state(user_id, "add_object_day", data)
        await update.message.reply_text(
            f"✅ Скидочный период: до *{discount_end_val}*\n\n"
            + OBJECT_FIELDS[5][1],
            parse_mode="Markdown",
        )
        return

    # ── Main OBJECT_FIELDS flow ───────────────────────────────
    state_list = [f[0] for f in OBJECT_FIELDS]
    if state not in state_list:
        return

    idx = state_list.index(state)
    field_key = FIELD_KEYS[idx]

    # Validation
    if field_key == "payment_day":
        try:
            day = int(value)
            if not (1 <= day <= 28):
                await update.message.reply_text("Введите число от 1 до 28.")
                return
        except ValueError:
            await update.message.reply_text("Введите корректное число (1-28).")
            return

    if field_key in ("lease_start", "lease_end"):
        try:
            datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            await update.message.reply_text(
                "⚠️ Неверный формат даты.\n\nВведите дату в формате *ДД.ММ.ГГГГ*\nНапример: *01.06.2025*",
                parse_mode="Markdown",
            )
            return

    if field_key == "rent_amount":
        try:
            _parse_amount(value)
        except ValueError:
            await update.message.reply_text("Введите сумму цифрами, например: 350 или 350.50")
            return

    data[field_key] = value

    # After rent_amount: ask about discount period
    if field_key == "rent_amount":
        set_state(user_id, "add_object_discount_q", data)
        await update.message.reply_text(
            f"💰 Стандартная аренда: *{value}*\n\n"
            "💡 Хотите установить скидочный период?\n"
            "_Например, первые 3 месяца по $250, потом стандартные $350_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да, добавить", callback_data="obj_discount_yes"),
                    InlineKeyboardButton("➡ Нет", callback_data="obj_discount_no"),
                ]
            ]),
        )
        return

    if idx + 1 < len(OBJECT_FIELDS):
        next_state, next_prompt = OBJECT_FIELDS[idx + 1]
        set_state(user_id, next_state, data)

        if field_key == "lease_start":
            await update.message.reply_text(
                next_prompt,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Без фиксированной даты окончания", callback_data="obj_no_end_date")]
                ]),
            )
        else:
            await update.message.reply_text(next_prompt, parse_mode="Markdown")
    else:
        await _save_object(update.message, user_id, data)


async def _save_object(msg, user_id: int, data: dict) -> None:
    settings = get_user_settings(user_id)
    data["currency"] = settings.get("currency", "USD")
    data["status"] = "rented"
    ok = await asyncio.to_thread(sheets.add_object, data)
    clear_state(user_id)
    sym = settings.get("symbol", "$")

    rent = data.get("rent_amount", "?")
    initial = data.get("initial_price", "")
    discount_end = data.get("discount_end", "")

    price_line = f"💰 Аренда: {sym}{rent}/мес."
    if initial and discount_end:
        price_line = f"💰 Аренда: {sym}{initial} (скидка до {discount_end}) → {sym}{rent}/мес."

    if ok:
        await msg.reply_text(
            f"✅ *{data['name']}* успешно добавлен!\n\n"
            f"📍 {data['address']}\n"
            f"👤 {data['tenant_name']} | {data['tenant_phone']}\n"
            f"{price_line}, день оплаты: {data['payment_day']}\n"
            f"📅 Договор: {data['lease_start']} → {data['lease_end']}\n\n"
            f"🔗 [Открыть в таблице]({sheets.spreadsheet_url()})",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await msg.reply_text(
            "⚠️ Сохранено локально (таблица недоступна, синхронизация выполнится автоматически).",
            reply_markup=main_menu_keyboard(),
        )


async def objects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    objects = await asyncio.to_thread(sheets.get_objects)
    if not objects:
        await msg.reply_text(
            "Объекты не найдены. Добавьте первый!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить объект", callback_data="menu_add_object")]
            ]),
        )
        return

    keyboard = []
    for obj in objects:
        status_icon = "🏠" if obj.get("status") == "rented" else "🔲"
        keyboard.append([
            InlineKeyboardButton(
                f"{status_icon} {obj.get('name')} — {obj.get('tenant_name', 'Вакантно')}",
                callback_data=f"obj_detail_{obj.get('id')}",
            )
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить объект", callback_data="menu_add_object")])

    occ = analytics.occupancy_rate()
    await msg.reply_text(
        f"🏠 *Мои объекты* ({occ['rented']}/{occ['total']} занято, {occ['rate']}%)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def object_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    obj_id = query.data.replace("obj_detail_", "")

    objects = await asyncio.to_thread(sheets.get_objects)
    obj = next((o for o in objects if str(o.get("id")) == obj_id), None)
    if not obj:
        await query.edit_message_text("Объект не найден.")
        return

    rel = analytics.payment_reliability(obj_id)
    settings = get_user_settings(query.from_user.id)
    sym = settings.get("symbol", "$")

    initial = obj.get("initial_price", "")
    discount_end = obj.get("discount_end", "")
    regular = obj.get("rent_amount", "")
    current = get_current_rent(obj)

    if initial and discount_end:
        price_text = f"💰 Аренда: {sym}{initial} (скидка до {discount_end}) → {sym}{regular}\n   Сейчас: {sym}{current}"
    else:
        price_text = f"💰 Аренда: {sym}{regular}/мес."

    text = (
        f"🏠 *{obj.get('name')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {obj.get('address')}\n"
        f"👤 Арендатор: {obj.get('tenant_name')}\n"
        f"📞 {obj.get('tenant_phone')}\n"
        f"{price_text}\n"
        f"📅 День оплаты: {obj.get('payment_day')} числа\n"
        f"📝 Договор: {obj.get('lease_start')} → {obj.get('lease_end')}\n"
        f"🔘 Статус: {_status_ru(obj.get('status', ''))}\n\n"
        f"📊 *Надёжность:* {rel['on_time_pct']}% вовремя "
        f"({rel['total']} платежей, {rel['late_count']} просрочено)\n"
        f"⏱ Средняя задержка: {rel['avg_delay_days']} дн."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Записать платёж", callback_data=f"pay_obj_{obj_id}"),
            InlineKeyboardButton("🔧 Записать расход", callback_data=f"exp_obj_{obj_id}"),
        ],
        [InlineKeyboardButton("◀ Назад к списку", callback_data="menu_objects")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


def _status_ru(status: str) -> str:
    return {"rented": "СДАЁТСЯ", "vacant": "ВАКАНТНО"}.get(status.lower(), status.upper())


async def obj_no_end_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state, data = get_state(user_id)

    data["lease_end"] = "бессрочно"
    settings = get_user_settings(user_id)
    data["currency"] = settings.get("currency", "USD")
    data["status"] = "rented"
    ok = await asyncio.to_thread(sheets.add_object, data)
    clear_state(user_id)

    if ok:
        sym = settings.get("symbol", "$")
        await query.edit_message_text(
            f"✅ *{data['name']}* успешно добавлен (бессрочный договор)!\n\n"
            f"🔗 [Открыть в таблице]({sheets.spreadsheet_url()})",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await query.edit_message_text(
            "⚠️ Сохранено локально (таблица недоступна, синхронизация выполнится).",
            reply_markup=main_menu_keyboard(),
        )


async def obj_discount_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    set_state(user_id, "add_object_initial_price", data)
    await query.edit_message_text(
        DISCOUNT_FIELDS[0][1] + "\n\n_Это цена в начале (меньшая, скидочная)_",
        parse_mode="Markdown",
    )


async def obj_discount_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["initial_price"] = ""
    data["discount_end"] = ""
    set_state(user_id, "add_object_day", data)
    await query.edit_message_text(
        OBJECT_FIELDS[5][1],
        parse_mode="Markdown",
    )
