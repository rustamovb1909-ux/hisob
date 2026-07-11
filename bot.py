# -*- coding: utf-8 -*-
"""
Hisobchi — Telegram bot (aiogram v3)
- Foydalanuvchi /start bosadi
- Ro'yxatdan o'tmagan bo'lsa: raqamini yuborishi so'raladi (tugma orqali, faqat OWN contact)
- Ro'yxatdan o'tgach: "Hisobchini ochish" Web App tugmasi beriladi
- Ro'yxatdan o'tmasdan boshqa hech narsa qila olmaydi
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

from app import get_user, upsert_user, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hisobchi-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # masalan: https://sizning-app.onrender.com

# MUHIM: Bot/Dispatcher'ni bu yerda global yaratmaymiz. Agar polling biror
# sababdan uzilib qayta ishga tushsa (yangi asyncio event loop bilan), eski
# Bot obyektining aiohttp sessiyasi eski (yopilgan) loop'ga bog'langan bo'lib
# qoladi va "RuntimeError: Event loop is closed" xatosini beradi. Shuning
# uchun handlerlarni Router'ga ro'yxatdan o'tkazamiz, Bot/Dispatcher esa har
# safar start_bot() chaqirilganda yangidan yaratiladi.
router = Router()


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📊 Hisobchini ochish",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = get_user(message.from_user.id)
    if user:
        await message.answer(
            f"Salom, <b>{message.from_user.first_name}</b>! 👋\n\n"
            "Hisobchi tayyor — kirim va xarajatlaringizni boshqarish uchun "
            "quyidagi tugmani bosing.",
            reply_markup=webapp_keyboard(),
        )
        return

    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "<b>Hisobchi</b> botiga xush kelibsiz — bu bot orqali kirim va "
        "xarajatlaringizni qulay tarzda hisoblab borishingiz mumkin.\n\n"
        "Davom etish uchun avval telefon raqamingizni tasdiqlashingiz kerak. "
        "Pastdagi tugmani bosing 👇",
        reply_markup=contact_keyboard(),
    )


@router.message(F.contact)
async def on_contact(message: Message):
    contact = message.contact
    # Xavfsizlik: faqat o'zining raqamini qabul qilamiz, boshqa userning
    # kontaktini forward qilib yuborishning oldini olamiz
    if contact.user_id != message.from_user.id:
        await message.answer(
            "❗️ Iltimos, faqat <b>o'zingizning</b> raqamingizni yuboring.",
            reply_markup=contact_keyboard(),
        )
        return

    upsert_user(
        telegram_id=message.from_user.id,
        phone=contact.phone_number,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        username=message.from_user.username,
    )

    await message.answer(
        "✅ Ro'yxatdan muvaffaqiyatli o'tdingiz!",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(
        "Endi Hisobchidan foydalanishingiz mumkin 👇",
        reply_markup=webapp_keyboard(),
    )


@router.message()
async def block_unregistered(message: Message):
    """Ro'yxatdan o'tmagan foydalanuvchi boshqa hech narsa yoza olmaydi —
    doim raqam so'raladi. Ro'yxatdan o'tganlar uchun esa webapp tugmasi
    qayta yuboriladi."""
    user = get_user(message.from_user.id)
    if user:
        await message.answer(
            "Hisobchini ochish uchun tugmani bosing 👇",
            reply_markup=webapp_keyboard(),
        )
    else:
        await message.answer(
            "Davom etish uchun avval telefon raqamingizni yuboring 👇",
            reply_markup=contact_keyboard(),
        )


async def _run_polling():
    """Har chaqirilganda TOZA Bot va Dispatcher yaratadi — shu joriy
    event loop'ga bog'langan aiohttp sessiya bilan. Shu tufayli qayta
    urinishlarda eski (yopilgan) loop'ga bog'langan sessiya ishlatilmaydi."""
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


def start_bot():
    """Alohida thread ichidan chaqiriladi (main.py orqali)."""
    init_db()
    asyncio.run(_run_polling())


if __name__ == "__main__":
    start_bot()
