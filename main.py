import os
import re
import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# ===== НАСТРОЙКИ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = set(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)
ADMIN_IDS.add(OWNER_ID)

# ===== ВСПОМОГАТЕЛЬНОЕ =====
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def parse_buttons(text: str):
    buttons = []
    for line in text.splitlines():
        if "-" in line:
            title, url = line.split("-", 1)
            title = title.strip()
            url = url.strip()
            if title and url.startswith("http"):
                buttons.append((title, url))
    return buttons

def build_kb(buttons):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, url=url)]
            for title, url in buttons
        ]
    )

# ===== СОСТОЯНИЯ =====
class Post(StatesGroup):
    text = State()
    buttons = State()
    preview = State()

# ===== БОТ =====
dp = Dispatcher()

@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Привет! Я бот для публикации постов в канал.\n\n"
        "Команды:\n"
        "/newpost — создать пост\n"
        "/myid — узнать свой ID\n"
        "/cancel — отмена"
    )

@dp.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Твой user_id: {m.from_user.id}")

@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Ок, отменено.")

@dp.message(Command("newpost"))
async def newpost(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    await state.set_state(Post.text)
    await m.answer("Пришли текст поста")

@dp.message(Post.text)
async def get_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text)
    await state.set_state(Post.buttons)
    await m.answer(
        "Теперь кнопки (по одной строке):\n"
        "Текст - https://example.com\n\n"
        "Если кнопки не нужны — напиши `нет`"
    )

@dp.message(Post.buttons)
async def get_buttons(m: Message, state: FSMContext):
    data = await state.get_data()
    text = data["text"]

    if m.text.lower() == "нет":
        buttons = []
    else:
        buttons = parse_buttons(m.text)

    kb = build_kb(buttons)
    await state.update_data(buttons=buttons)
    await state.set_state(Post.preview)

    await m.answer("Предпросмотр:")
    await m.answer(text, reply_markup=kb)
    await m.answer("Напиши `опубликовать` или `отмена`")

@dp.message(Post.prev
