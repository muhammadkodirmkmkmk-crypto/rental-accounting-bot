"""
Voice message handler.
Flow: Telegram OGG → faster-whisper transcription → free_text_handler → Claude AI
"""
import asyncio
import logging
import os
import tempfile

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
    segments, info = model.transcribe(ogg_path, beam_size=5)
    text = " ".join(s.text for s in segments).strip()
    logger.info("Whisper transcription (lang=%s): %r", info.language, text)
    return text


async def voice_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Transcribe voice → pass text to free_text_handler which calls Claude AI.
    No old-style NLP or separate Claude parse — the full AI brain handles everything.
    """
    from handlers.text import free_text_handler

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
            transcription = await asyncio.to_thread(_transcribe_audio, ogg_path)
        except Exception as e:
            logger.error("Whisper transcription failed: %s", e)
            await update.message.reply_text(
                "❌ Не удалось распознать речь. Говорите чётче или напишите текстом."
            )
            return

    if not transcription.strip():
        await update.message.reply_text(
            "🤷 Не удалось разобрать речь. Попробуйте ещё раз."
        )
        return

    # Show what was heard
    await update.message.reply_text(
        f"🗣 *Распознано:* _{transcription}_",
        parse_mode="Markdown",
    )

    # Hand off directly to the Claude AI handler — it handles everything
    await free_text_handler(update, context, text=transcription)
