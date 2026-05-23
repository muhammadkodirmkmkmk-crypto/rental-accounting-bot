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

# ── Wizard steps ──────────────────────────────────────────────────────────────
# Required: name, tenant_name, rent_amount, payment_day, lease_start
# Optional: tenant_phone (skip), address (skip), lease_end (бессрочно)
# Discount: asked at the very end before saving

OBJECT_FIELDS = [
    ("add_object_name",    "🏠 *Название объекта*\n_Например: Квартира 1, Офис на Ленина_"),
    ("add_object_tenant",  "👤 *ФИО арендатора*"),
    ("add_object_phone",   "📞 *Телефон арендатора*"),
    ("add_object_address", "📍 *Адрес объекта*"),
    ("add_object_rent",    "💰 *Сумма аренды в месяц* (только цифры, например: 350)"),
    ("add_object_day",     "📅 *День оплаты каждого месяца* (1-28)"),
    ("add_object_start",   "📅 *Дата начала аренды*\n_Формат: ДД.ММ.ГГГГ, например: 01.06.2025_"),
    ("add_object_end",     "📅 *Дата окончания аренды*\n_Формат: ДД.ММ.ГГГГ_"),
]

DISCOUNT_FIELDS = [
    ("add_object_initial_price", "💵 *Скидочная цена в начале* (только цифры):"),
    ("add_object_discount_end",  "📅 *До когда действует скидка?*\n_ДД.ММ.ГГГГ или кол-во месяцев (3)_"),
]

ALL_TEXT_STATES = (
    {f[0] for f in OBJECT_FIELDS}
    | {f[0] for f in DISCOUNT_FIELDS}
    | {"add_object_discount_q"}
)

FIELD_KEYS = [
    "name", "tenant_name", "tenant_phone", "address",
    "rent_amount", "payment_day", "lease_start", "lease_end",
]

# Fields that can be skipped with a button
SKIPPABLE = {
    "add_object_phone":   ("tenant_phone", "—"),
    "add_object_address": ("address",      "—"),
}

_SKIP_KB = InlineKeyboardMarkup([[InlineKeyboardButton("➡ Пропустить", callback_data="obj_skip_field")]])
_VACANT_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔲 Вакантно (нет арендатора)", callback_data="obj_vacant")]])


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


def _progress(state_name: str) -> str:
    """Return a step indicator like '(3/8)'."""
    names = [f[0] for f in OBJECT_FIELDS]
    try:
        idx = names.index(state_name)
        return f"({idx + 1}/{len(OBJECT_FIELDS)})"
    except ValueError:
        return ""


# ── Start wizard ──────────────────────────────────────────────────────────────

async def add_object_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_state(user_id)
    set_state(user_id, "add_object_name", {})
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(
            f"🏠 *Добавляем новый объект* (1/{len(OBJECT_FIELDS)})\n\n"
            + OBJECT_FIELDS[0][1],
            parse_mode="Markdown",
        )


async def start_add_object_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await add_object_command(update, context)


# ── Main text handler ─────────────────────────────────────────────────────────

async def add_object_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state, data = get_state(user_id)

    if state not in ALL_TEXT_STATES:
        return

    value = update.message.text.strip()

    # ── Discount Q state: user typed instead of pressing button ──
    if state == "add_object_discount_q":
        low = value.lower()
        if low in ("да", "yes", "д"):
            set_state(user_id, "add_object_initial_price", data)
            await update.message.reply_text(
                DISCOUNT_FIELDS[0][1] + "\n\n_Это сниженная цена на первые месяцы_",
                parse_mode="Markdown",
            )
        else:
            data.setdefault("initial_price", "")
            data.setdefault("discount_end", "")
            await _save_object(update.message, user_id, data)
        return

    # ── Discount flow ─────────────────────────────────────────────
    if state == "add_object_initial_price":
        try:
            initial = _parse_amount(value)
        except ValueError:
            await update.message.reply_text("Введите сумму цифрами, например: 250")
            return
        data["initial_price"] = str(initial)
        set_state(user_id, "add_object_discount_end", data)
        await update.message.reply_text(
            DISCOUNT_FIELDS[1][1],
            parse_mode="Markdown",
        )
        return

    if state == "add_object_discount_end":
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
                from dateutil.relativedelta import relativedelta as rd
                discount_end_val = (date.today() + rd(months=months)).strftime("%d.%m.%Y")
        else:
            try:
                datetime.strptime(value, "%d.%m.%Y")
                discount_end_val = value
            except ValueError:
                await update.message.reply_text(
                    "Введите дату (ДД.ММ.ГГГГ) или число месяцев (например: 3)."
                )
                return
        data["discount_end"] = discount_end_val
        await _save_object(update.message, user_id, data)
        return

    # ── Main OBJECT_FIELDS flow ───────────────────────────────────
    state_list = [f[0] for f in OBJECT_FIELDS]
    if state not in state_list:
        return

    idx = state_list.index(state)
    field_key = FIELD_KEYS[idx]

    # ── Field validations ─────────────────────────────────────────
    if field_key == "payment_day":
        try:
            day = int(value)
            if not (1 <= day <= 28):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите число от 1 до 28, например: 5")
            return

    if field_key in ("lease_start", "lease_end"):
        try:
            datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            await update.message.reply_text(
                "⚠️ Неверный формат даты. Введите в формате *ДД.ММ.ГГГГ*\nНапример: *01.06.2025*",
                parse_mode="Markdown",
            )
            return

    if field_key == "rent_amount":
        try:
            parsed_rent = _parse_amount(value)
        except ValueError:
            await update.message.reply_text("Введите сумму цифрами, например: 350 или 350.50")
            return
        data[field_key] = str(parsed_rent)
    else:
        data[field_key] = value

    # ── Advance to next step ──────────────────────────────────────
    await _next_step(update.message, user_id, data, idx)


async def _next_step(msg, user_id: int, data: dict, current_idx: int) -> None:
    """Move to the next wizard step, or ask discount question before saving."""
    state_list = [f[0] for f in OBJECT_FIELDS]
    next_idx = current_idx + 1

    if next_idx < len(OBJECT_FIELDS):
        next_state, next_prompt = OBJECT_FIELDS[next_idx]
        set_state(user_id, next_state, data)

        # Build appropriate keyboard for the next step
        kb = None
        if next_state in SKIPPABLE:
            kb = _SKIP_KB
        elif next_state == "add_object_tenant":
            kb = _VACANT_KB
        elif next_state == "add_object_end":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("♾ Бессрочный договор", callback_data="obj_no_end_date")
            ]])

        step_num = next_idx + 1
        total = len(OBJECT_FIELDS)
        prefix = f"*({step_num}/{total})* " if next_state != "add_object_name" else ""

        await msg.reply_text(
            prefix + next_prompt,
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        # All fields collected — ask discount question before saving
        await _ask_discount(msg, user_id, data)


async def _ask_discount(msg, user_id: int, data: dict) -> None:
    """Ask whether to add a discounted first-period price."""
    set_state(user_id, "add_object_discount_q", data)
    settings = get_user_settings(user_id)
    sym = settings.get("symbol", "$")
    rent = data.get("rent_amount", "?")
    await msg.reply_text(
        f"💰 Стандартная аренда: *{sym}{rent}/мес.*\n\n"
        "💡 Хотите добавить скидочный период?\n"
        "_Например, первые 3 месяца по $250, потом $350_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, добавить скидку", callback_data="obj_discount_yes"),
                InlineKeyboardButton("➡ Нет", callback_data="obj_discount_no"),
            ]
        ]),
    )


async def _save_object(msg, user_id: int, data: dict) -> None:
    settings = get_user_settings(user_id)
    data["currency"] = settings.get("currency", "USD")
    data.setdefault("status", "rented")
    data.setdefault("initial_price", "")
    data.setdefault("discount_end", "")
    ok = await asyncio.to_thread(sheets.add_object, data)
    clear_state(user_id)
    sym = settings.get("symbol", "$")

    rent = data.get("rent_amount", "?")
    initial = data.get("initial_price", "")
    discount_end = data.get("discount_end", "")
    tenant = data.get("tenant_name", "Вакантно")
    phone = data.get("tenant_phone", "")
    address = data.get("address", "")

    price_line = f"💰 Аренда: {sym}{rent}/мес."
    if initial and discount_end:
        price_line = f"💰 Аренда: {sym}{initial} (скидка до {discount_end}) → {sym}{rent}/мес."

    info_lines = [
        f"🏠 *{data['name']}*",
        f"📍 {address}" if address and address != "—" else None,
        f"👤 {tenant}" + (f"  |  📞 {phone}" if phone and phone != "—" else ""),
        price_line,
        f"📅 День оплаты: {data.get('payment_day')} числа",
        f"📝 Договор: {data.get('lease_start')} → {data.get('lease_end', '—')}",
    ]
    info_text = "\n".join(l for l in info_lines if l)

    if ok:
        await msg.reply_text(
            f"Амирхон ака, объект добавлен! ✅\n\n{info_text}\n\n"
            f"🔗 [Открыть в таблице]({sheets.spreadsheet_url()})",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await msg.reply_text(
            "Амирхон ака, сохранено локально ⚠️\n(таблица недоступна, синхронизация выполнится автоматически).",
            reply_markup=main_menu_keyboard(),
        )


# ── Callback handlers ─────────────────────────────────────────────────────────

async def obj_skip_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User pressed 'Пропустить' for an optional field."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    state, data = get_state(user_id)

    if state in SKIPPABLE:
        field_key, placeholder = SKIPPABLE[state]
        data[field_key] = placeholder
        state_list = [f[0] for f in OBJECT_FIELDS]
        idx = state_list.index(state)
        await _next_step(query.message, user_id, data, idx)
    else:
        await query.message.reply_text("Нельзя пропустить это поле.")


async def obj_vacant_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User pressed 'Вакантно' for tenant name."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["tenant_name"] = "Вакантно"
    data["status"] = "vacant"
    state_list = [f[0] for f in OBJECT_FIELDS]
    idx = state_list.index("add_object_tenant")
    await _next_step(query.message, user_id, data, idx)


async def obj_no_end_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["lease_end"] = "бессрочно"
    await _ask_discount(query.message, user_id, data)


async def obj_discount_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    set_state(user_id, "add_object_initial_price", data)
    await query.message.reply_text(
        DISCOUNT_FIELDS[0][1] + "\n\n_Это сниженная цена на первые месяцы_",
        parse_mode="Markdown",
    )


async def obj_discount_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, data = get_state(user_id)
    data["initial_price"] = ""
    data["discount_end"] = ""
    await _save_object(query.message, user_id, data)


# ── Object list & detail ──────────────────────────────────────────────────────

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

    address = obj.get("address", "")
    addr_line = f"📍 {address}\n" if address and address != "—" else ""
    phone = obj.get("tenant_phone", "")
    phone_line = f"  📞 {phone}" if phone and phone != "—" else ""

    text = (
        f"🏠 *{obj.get('name')}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{addr_line}"
        f"👤 {obj.get('tenant_name')}{phone_line}\n"
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
