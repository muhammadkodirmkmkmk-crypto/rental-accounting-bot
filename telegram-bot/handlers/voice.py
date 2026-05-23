"""
Voice message handler.
Flow: Telegram OGG → Whisper transcription → Claude NLP → free_text_handler
"""
import logging
import os
import tempfile
import json

from telegram import Update
from telegram.ext import ContextTypes

import config

logger = logging.getLogger(__name__)

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading faster-whisper model '%s'...", config.WHISPER_MODEL)
        _whisper_model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
        logger.info("Whisper model loaded.")
    return _whisper_model


def _transcribe_audio(ogg_path: str) -> str:
    model = _get_whisper_model()
    segments, _ = model.transcribe(ogg_path, beam_size=5)
    text = " ".join(s.text for s in segments).strip()
    logger.info("Whisper transcription: %r", text)
    return text


def _claude_parse(transcription: str, known_objects: list[str]) -> dict | None:
    """Use Claude to extract structured intent from a transcription."""
    if not config.ANTHROPIC_API_KEY:
        return None

    import anthropic

    objects_str = ", ".join(known_objects) if known_objects else "нет объектов"

    prompt = f"""Ты — ассистент по учёту аренды недвижимости. Пользователь отправил голосовое сообщение. Твоя задача — распознать намерение и извлечь данные.

Список объектов пользователя: {objects_str}

Голосовое сообщение (транскрипция): "{transcription}"

Определи намерение (intent):
- "record_payment" — если речь о получении арендной платы
- "record_expense" — если речь о расходе/трате
- "report" — если запрашивается отчёт

Ответь ТОЛЬКО валидным JSON без дополнительного текста. Формат:
{{
  "intent": "record_payment" | "record_expense" | "report" | null,
  "object_name": "название объекта или null",
  "amount": число или null,
  "category": "repair|utilities|tax|insurance|management|advertising|other или null",
  "month": число 1-12 или null,
  "year": число или null,
  "confidence": "high" | "medium" | "low"
}}

Если намерение неясно — верни intent: null.
Суммы могут быть в долларах, рублях, тенге и т.д. — верни только число.
"""

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if not raw:
            logger.warning("Claude returned empty response")
            return None
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning("No JSON object found in Claude response: %r", raw)
            return None
        parsed = json.loads(raw[start:end])
        logger.info("Claude NLP result: %s", parsed)
        return parsed
    except Exception as e:
        logger.warning("Claude NLP failed: %s", e)
        return None


async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.text import free_text_handler
    import sheets
    import nlp as nlp_module

    voice = update.message.voice
    if not voice:
        return

    await update.message.reply_text("🎙 Распознаю голосовое сообщение...")

    with tempfile.TemporaryDirectory() as tmpdir:
        ogg_path = os.path.join(tmpdir, "voice.ogg")

        try:
            tg_file = await context.bot.get_file(voice.file_id)
            await tg_file.download_to_drive(ogg_path)
        except Exception as e:
            logger.error("Failed to download voice file: %s", e)
            await update.message.reply_text(
                "❌ Не удалось загрузить голосовое сообщение. Попробуйте ещё раз."
            )
            return

        try:
            transcription = _transcribe_audio(ogg_path)
        except Exception as e:
            logger.error("Whisper transcription failed: %s", e)
            await update.message.reply_text(
                "❌ Не удалось распознать речь. Попробуйте говорить чётче или используйте текстовые команды."
            )
            return

    if not transcription:
        await update.message.reply_text(
            "🤷 Не удалось разобрать речь. Попробуйте ещё раз или используйте текстовые команды."
        )
        return

    await update.message.reply_text(f"🗣 *Распознано:* _{transcription}_", parse_mode="Markdown")

    known_objects = [o.get("name", "") for o in sheets.get_objects()]

    claude_result = _claude_parse(transcription, known_objects)

    if claude_result and claude_result.get("intent") and claude_result.get("confidence") in ("high", "medium"):
        from datetime import datetime
        now = datetime.now()

        parsed = {
            "intent": claude_result.get("intent"),
            "object_name": claude_result.get("object_name"),
            "amount": claude_result.get("amount"),
            "category": claude_result.get("category") or "other",
            "month": claude_result.get("month") or now.month,
            "year": claude_result.get("year") or now.year,
        }

        original_text = update.message.text
        update.message.text = transcription

        try:
            await free_text_handler(update, context, text=transcription)
        except Exception as e:
            logger.error("Error processing voice command via Claude: %s", e)
            update.message.text = original_text
            await free_text_handler(update, context, text=transcription)
        return

    nlp_parsed = nlp_module.parse_free_text(transcription, known_objects)
    if nlp_parsed:
        await free_text_handler(update, context, text=transcription)
    else:
        from handlers.start import main_menu_keyboard
        await update.message.reply_text(
            "🤔 Не смог определить команду. Попробуйте сказать, например:\n\n"
            "• «Квартира 1 заплатила 300 долларов»\n"
            "• «Записать расход 150 на ремонт в офисе»\n"
            "• «Отчёт за май»",
            reply_markup=main_menu_keyboard(),
        )
