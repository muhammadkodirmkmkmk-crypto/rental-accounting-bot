import logging
from datetime import datetime
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
    ("add_object_rent",    "💰 *Сумма аренды в месяц* (только цифры):"),
    ("add_object_day",     "📅 *День оплаты* (1-28):"),
    ("add_object_start",   "📅 *Дата начала аренды* (ДД.ММ.ГГГГ):"),
    ("add_object_end",     "📅 *Дата окончания аренды* (ДД.ММ.ГГГГ):"),
]

FIELD_KEYS = [
    "name", "address", "tenant_name", "tenant_phone",
    "rent_amount", "payment_day", "lease_start", "lease_end",
]


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

    state_list = [f[0] for f in OBJECT_FIELDS]
    if state not in state_list:
        return

    idx = state_list.index(state)
    field_key = FIELD_KEYS[idx]
    value = update.message.text.strip()

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
            await update.message.reply_text("Используйте формат ДД.ММ.ГГГГ (например: 01.06.2025).")
            return

    data[field_key] = value

    if idx + 1 < len(OBJECT_FIELDS):
        next_state, next_prompt = OBJECT_FIELDS[idx + 1]
        set_state(user_id, next_state, data)

        if field_key == "lease_start":
            await update.message.reply_text(next_prompt, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Без фиксированной даты окончания", callback_data="obj_no_end_date")]
                ]))
        else:
            await update.message.reply_text(next_prompt, parse_mode="Markdown")
    else:
        settings = get_user_settings(user_id)
        data["currency"] = settings.get("currency", "USD")
        data["status"] = "rented"
        ok = sheets.add_object(data)
        clear_state(user_id)
        if ok:
            sym = settings.get("symbol", "$")
            await update.message.reply_text(
                f"✅ *{data['name']}* успешно добавлен!\n\n"
                f"📍 {data['address']}\n"
                f"👤 {data['tenant_name']} | {data['tenant_phone']}\n"
                f"💰 Аренда: {sym}{data['rent_amount']}/мес., день оплаты: {data['payment_day']}\n"
                f"📅 Договор: {data['lease_start']} → {data['lease_end']}\n\n"
                f"🔗 [Открыть в таблице]({sheets.spreadsheet_url()})",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await update.message.reply_text(
                "⚠️ Сохранено локально (таблица недоступна, синхронизация выполнится автоматически).",
                reply_markup=main_menu_keyboard(),
            )


async def objects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message

    objects = sheets.get_objects()
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

    objects = sheets.get_objects()
    obj = next((o for o in objects if str(o.get("id")) == obj_id), None)
    if not obj:
        await query.edit_message_text("Объект не найден.")
        return

    rel = analytics.payment_reliability(obj_id)
    settings = get_user_settings(query.from_user.id)
    sym = settings.get("symbol", "$")

    text = (
        f"🏠 *{obj.get('name')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {obj.get('address')}\n"
        f"👤 Арендатор: {obj.get('tenant_name')}\n"
        f"📞 {obj.get('tenant_phone')}\n"
        f"💰 Аренда: {sym}{obj.get('rent_amount')}/{obj.get('currency')}\n"
        f"📅 День оплаты: {obj.get('payment_day')} числа каждого месяца\n"
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
    ok = sheets.add_object(data)
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
