import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

TITLE = "💬 Подслушано РАНХиГС | Волгоград"
FOOTER = "#подслушано"


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str, limit: int = 3000) -> list[str]:
    chunks, chunk, units = [], [], 0
    for char in text:
        size = utf16_length(char)
        if units + size > limit:
            chunks.append("".join(chunk))
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        chunks.append("".join(chunk))
    return chunks


def channel_text(text: str) -> str:
    if utf16_length(f"{TITLE}\n\n{text}\n\n{FOOTER}") > 4096:
        raise ValueError("Текст с оформлением превышает лимит Telegram")
    return f"<b>{TITLE}</b>\n\n{html.escape(text)}\n\n{FOOTER}"


def moderation_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod:publish:{submission_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:reject:{submission_id}"),
            ],
            [InlineKeyboardButton(text="🚫 Заблокировать автора", callback_data=f"mod:ban:{submission_id}")],
        ]
    )
