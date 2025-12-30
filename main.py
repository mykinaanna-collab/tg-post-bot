import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import asyncpg


# ================== ENV ==================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip())
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()

TIMEZONE = (os.getenv("TIMEZONE") or "Europe/Moscow").strip()
TZ = ZoneInfo(TIMEZONE)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# (optional) old admins from ENV
ENV_ADMINS = set(
    int(x.strip()) for x in (os.getenv("ADMIN_IDS", "") or "").split(",") if x.strip().isdigit()
)

# Telegram limits (Bot API)
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


# ================== UI ==================
BTN_NEWPOST = "📝 Новый пост"
BTN_JOBS = "📅 Запланированные"
BTN_POSTS = "🧾 Опубликованные"
BTN_MYID = "👤 Мой ID"
BTN_CANCEL = "❌ Отмена"
BTN_ADMINS = "⚙️ Админы"
BTN_HELP = "❓ Помощь"

def admin_menu_kb(is_owner: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_NEWPOST)],
        [KeyboardButton(text=BTN_JOBS), KeyboardButton(text=BTN_POSTS)],
        [KeyboardButton(text=BTN_MYID), KeyboardButton(text=BTN_CANCEL)],
    ]
    if is_owner:
        rows.insert(2, [KeyboardButton(text=BTN_ADMINS), KeyboardButton(text=BTN_HELP)])
    else:
        rows.insert(2, [KeyboardButton(text=BTN_HELP)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ================== HELPERS ==================
def now_tz() -> datetime:
    return datetime.now(TZ)

def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

def tz_label() -> str:
    if TIMEZONE == "Europe/Moscow":
        return "МСК"
    if TIMEZONE == "Europe/Riga":
        return "Рига"
    return TIMEZONE

def parse_buttons(text: str):
    """
    Lines:
      Text - https://example.com
    """
    buttons = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        seps = [" - ", " — ", " – ", " | "]
        sep_found = None
        for sep in seps:
            if sep in line:
                sep_found = sep
                break

        if sep_found:
            title, url = line.split(sep_found, 1)
        elif "-" in line:
            title, url = line.split("-", 1)
        else:
            continue

        title = title.strip()
        url = url.strip()
        if title and url.startswith(("http://", "https://")):
            buttons.append((title[:64], url))
    return buttons

def build_kb(buttons):
    if not buttons:
        return None
    rows = []
    for title, url in buttons:
        rows.append([InlineKeyboardButton(text=title, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def preview_actions_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать сейчас", callback_data="draft:pub_now")],
        [InlineKeyboardButton(text="📅 Запланировать", callback_data="draft:schedule")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="draft:cancel")],
    ])

def parse_dt_local(s: str) -> datetime:
    dt = datetime.strptime(s.strip(), "%d.%m.%Y %H:%M")
    return dt.replace(tzinfo=TZ)

def quick_times_kb(prefix: str, entity_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕛 Сегодня 12:00", callback_data=f"{prefix}:{entity_id}:today12")],
        [InlineKeyboardButton(text="🕑 Сегодня 14:00", callback_data=f"{prefix}:{entity_id}:today14")],
        [InlineKeyboardButton(text="🕔 Сегодня 17:00", callback_data=f"{prefix}:{entity_id}:today17")],
        [InlineKeyboardButton(text="🕛 Завтра 12:00", callback_data=f"{prefix}:{entity_id}:tom12")],
        [InlineKeyboardButton(text="🕑 Завтра 14:00", callback_data=f"{prefix}:{entity_id}:tom14")],
        [InlineKeyboardButton(text="🕔 Завтра 17:00", callback_data=f"{prefix}:{entity_id}:tom17")],
        [InlineKeyboardButton(text="🗓 Ввести вручную", callback_data=f"{prefix}:{entity_id}:manual")],
    ])

def calc_quick_dt(code: str) -> datetime:
    n = now_tz()
    today = n.date()
    tomorrow = (n + timedelta(days=1)).date()

    def at(d, h):
        return datetime(d.year, d.month, d.day, h, 0, tzinfo=TZ)

    mapping = {
        "today12": at(today, 12),
        "today14": at(today, 14),
        "today17": at(today, 17),
        "tom12": at(tomorrow, 12),
        "tom14": at(tomorrow, 14),
        "tom17": at(tomorrow, 17),
    }
    return mapping[code]

def admin_display(row: asyncpg.Record) -> str:
    uid = row["user_id"]
    username = row["username"]
    name = row["name"]
    if username:
        return f"@{username} ({uid})"
    if name:
        return f"{name} ({uid})"
    return str(uid)

def caption_too_long(text: str) -> bool:
    return len((text or "")) > CAPTION_LIMIT

def text_too_long(text: str) -> bool:
    return len((text or "")) > TEXT_LIMIT


# ================== DB ==================
POOL: asyncpg.Pool | None = None

def _need_ssl_from_url(url: str) -> bool:
    return "sslmode=require" in url.lower()

async def init_db():
    global POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty. Set it in Render → Environment.")

ssl = True if _need_ssl_from_url(DATABASE_URL) else None
POOL = await asyncpg.create_pool(
    DATABASE_URL,
    ssl=ssl,
    min_size=1,
    max_size=5,
    timeout=10,            # важно: чтобы не “висеть”
    command_timeout=30     # на всякий случай
)

async with POOL.acquire() as conn:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            name TEXT,
            added_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                text TEXT NOT NULL,
                buttons_json TEXT NOT NULL,
                photo_file_id TEXT,
                run_at TIMESTAMPTZ NOT NULL,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                message_id BIGINT NOT NULL,
                text_msg_id BIGINT,
                text TEXT NOT NULL,
                buttons_json TEXT NOT NULL,
                photo_file_id TEXT,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # OWNER always admin
        if OWNER_ID:
            await conn.execute("""
                INSERT INTO admins (user_id, username, name)
                VALUES ($1, NULL, 'OWNER')
                ON CONFLICT (user_id) DO NOTHING;
            """, OWNER_ID)

        # ENV admins
        for uid in ENV_ADMINS:
            if uid == OWNER_ID:
                continue
            await conn.execute("""
                INSERT INTO admins (user_id, username, name)
                VALUES ($1, NULL, NULL)
                ON CONFLICT (user_id) DO NOTHING;
            """, uid)

async def db_is_admin(user_id: int) -> bool:
    assert POOL is not None
    async with POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM admins WHERE user_id=$1", user_id)
        return row is not None

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ================== INLINE CONTROLS ==================
def post_controls_kb(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"post:edit:{post_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"post:del:{post_id}"),
        ]
    ])

def post_delete_confirm_kb(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"post:del_yes:{post_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"post:del_no:{post_id}"),
        ]
    ])

def job_controls_kb(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👁 Посмотреть", callback_data=f"job:view:{job_id}"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"job:edit:{job_id}"),
        ],
        [
            InlineKeyboardButton(text="🕒 Перенести", callback_data=f"job:move:{job_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"job:del:{job_id}"),
        ]
    ])

def job_delete_confirm_kb(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"job:del_yes:{job_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"job:del_no:{job_id}"),
        ]
    ])


# ================== FSM ==================
class CreatePost(StatesGroup):
    text = State()
    buttons = State()
    photo = State()
    long_with_photo_choice = State()
    preview = State()
    schedule_manual = State()

class EditPost(StatesGroup):
    text = State()
    buttons = State()
    photo = State()
    long_with_photo_choice = State()
    preview = State()

class EditJob(StatesGroup):
    text = State()
    buttons = State()
    photo = State()
    long_with_photo_choice = State()
    preview = State()

class MoveJob(StatesGroup):
    manual = State()


# ================== BOT ==================
dp = Dispatcher()


# ---------- COMMON ----------
@dp.message(Command("start"))
async def start(m: Message):
    uid = m.from_user.id
    if await db_is_admin(uid):
        await m.answer(
            "Привет! Меню доступно админам.\nНажми кнопки ниже 👇",
            reply_markup=admin_menu_kb(is_owner(uid))
        )
    else:
        await m.answer(
            "Привет! Я бот для публикации постов в канал.\n"
            "Если тебе нужен доступ — попроси владельца добавить тебя в админы.\n\n"
            "Команда для тебя:\n"
            "/myid — узнать свой user_id",
            reply_markup=ReplyKeyboardRemove()
        )

@dp.message(Command("menu"))
async def menu(m: Message):
    uid = m.from_user.id
    if not await db_is_admin(uid):
        return await m.answer("Меню доступно только админам.")
    await m.answer("Меню 👇", reply_markup=admin_menu_kb(is_owner(uid)))

@dp.message(Command("myid"))
async def myid(m: Message):
    uid = m.from_user.id
    isadm = await db_is_admin(uid)
    await m.answer(
        "Диагностика:\n"
        f"- твой user_id: {uid}\n"
        f"- ты админ по мнению бота: {isadm}\n"
        f"- TIMEZONE: {TIMEZONE}\n"
        f"- CHANNEL_ID: {CHANNEL_ID!r}\n"
        f"- DB: {'ok' if bool(DATABASE_URL) else 'missing'}\n"
    )

@dp.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    if await db_is_admin(m.from_user.id):
        await m.answer("Ок, отменено.", reply_markup=admin_menu_kb(is_owner(m.from_user.id)))
    else:
        await m.answer("Ок, отменено.", reply_markup=ReplyKeyboardRemove())


# ---------- MENU BUTTONS ----------
@dp.message(F.text == BTN_MYID)
async def menu_myid(m: Message):
    await myid(m)

@dp.message(F.text == BTN_CANCEL)
async def menu_cancel(m: Message, state: FSMContext):
    await cancel_cmd(m, state)

@dp.message(F.text == BTN_HELP)
async def menu_help(m: Message):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Доступ к функциям — только админам.")
    await m.answer(
        "Что умею:\n"
        "• 📝 Новый пост (текст + кнопки + фото)\n"
        "• 📅 Запланированные (посмотреть/редактировать/перенести/удалить)\n"
        "• 🧾 Опубликованные (редактировать/удалить)\n\n"
        "Если меню пропало — /menu",
        reply_markup=admin_menu_kb(is_owner(m.from_user.id))
    )


# ---------- ADMIN MGMT (OWNER) ----------
@dp.message(F.text == BTN_ADMINS)
async def menu_admins(m: Message):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None
    async with POOL.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM admins ORDER BY user_id ASC")
    lines = [admin_display(r) for r in rows]
    await m.answer("Админы:\n" + "\n".join(lines))

@dp.message(Command("admins"))
async def cmd_admins(m: Message):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None
    async with POOL.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM admins ORDER BY user_id ASC")
    lines = [admin_display(r) for r in rows]
    await m.answer("Админы:\n" + "\n".join(lines))

@dp.message(Command("addadmin"))
async def cmd_addadmin(m: Message, bot: Bot):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    parts = (m.text or "").split()

    if len(parts) == 2 and parts[1].isdigit():
        uid = int(parts[1])
        username = None
        name = None
        try:
            ch = await bot.get_chat(uid)
            username = getattr(ch, "username", None)
            first = getattr(ch, "first_name", None) or ""
            last = getattr(ch, "last_name", None) or ""
            name = (first + " " + last).strip() or None
        except Exception:
            pass

        async with POOL.acquire() as conn:
            await conn.execute("""
                INSERT INTO admins (user_id, username, name)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE
                SET username=EXCLUDED.username,
                    name=EXCLUDED.name;
            """, uid, username, name)

        disp = f"@{username} ({uid})" if username else (f"{name} ({uid})" if name else str(uid))
        return await m.answer(f"✅ Добавила админа: {disp}")

    if m.reply_to_message:
        if m.reply_to_message.forward_from:
            u = m.reply_to_message.forward_from
            uid = u.id
            username = getattr(u, "username", None)
            name = (f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}").strip() or None

            async with POOL.acquire() as conn:
                await conn.execute("""
                    INSERT INTO admins (user_id, username, name)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE
                    SET username=EXCLUDED.username,
                        name=EXCLUDED.name;
                """, uid, username, name)

            disp = f"@{username} ({uid})" if username else (f"{name} ({uid})" if name else str(uid))
            return await m.answer(f"✅ Добавила админа по пересланному сообщению: {disp}")

        if m.reply_to_message.from_user and m.reply_to_message.from_user.id == m.from_user.id:
            return await m.answer(
                "Не могу определить сотрудника по пересылке — Telegram скрывает автора (privacy).\n\n"
                "Варианты:\n"
                "1) Пусть сотрудник напишет боту /myid и пришлёт тебе цифры → /addadmin 123\n"
                "2) Или сотрудник может временно разрешить показывать автора при пересылке."
            )

        if m.reply_to_message.from_user:
            u = m.reply_to_message.from_user
            uid = u.id
            username = getattr(u, "username", None)
            name = (f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}").strip() or None

            async with POOL.acquire() as conn:
                await conn.execute("""
                    INSERT INTO admins (user_id, username, name)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE
                    SET username=EXCLUDED.username,
                        name=EXCLUDED.name;
                """, uid, username, name)

            disp = f"@{username} ({uid})" if username else (f"{name} ({uid})" if name else str(uid))
            return await m.answer(f"✅ Добавила админа по сообщению: {disp}")

    await m.answer(
        "Как добавить админа:\n"
        "1) /addadmin 123456789\n"
        "или\n"
        "2) Перешли сообщение сотрудника → ответь на него командой /addadmin\n\n"
        "Если Telegram скрывает автора пересылки — попроси у сотрудника /myid."
    )

@dp.message(Command("deladmin"))
async def cmd_deladmin(m: Message):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Использование: /deladmin 123456789")
    uid = int(parts[1])
    if uid == OWNER_ID:
        return await m.answer("OWNER удалить нельзя 🙂")

    async with POOL.acquire() as conn:
        res = await conn.execute("DELETE FROM admins WHERE user_id=$1", uid)

    if res.startswith("DELETE 1"):
        await m.answer(f"✅ Удалила админа: {uid}")
    else:
        await m.answer("Такого админа нет.")


# ---------- CREATE POST ----------
@dp.message(F.text == BTN_NEWPOST)
async def menu_newpost(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    await state.clear()
    await state.set_state(CreatePost.text)
    await m.answer("Пришли текст поста.")

@dp.message(Command("newpost"))
async def cmd_newpost(m: Message, state: FSMContext):
    await menu_newpost(m, state)

@dp.message(CreatePost.text)
async def create_get_text(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    text = (m.text or "").strip()
    if not text:
        return await m.answer("Нужен текст поста.")
    await state.update_data(text=text)
    await state.set_state(CreatePost.buttons)
    await m.answer(
        "Теперь кнопки (по одной строке):\n"
        "Текст - https://example.com\n\n"
        "Если кнопки не нужны — напиши `нет`",
        parse_mode="Markdown"
    )

@dp.message(CreatePost.buttons)
async def create_get_buttons(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")

    raw = (m.text or "").strip()
    buttons = [] if raw.lower() == "нет" else parse_buttons(raw)

    await state.update_data(buttons=buttons)
    await state.set_state(CreatePost.photo)
    await m.answer(
        "Теперь пришли ОДНО фото для поста.\n"
        "Если фото не нужно — напиши `нет`.",
        parse_mode="Markdown"
    )

@dp.message(CreatePost.photo)
async def create_get_photo(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])

    raw = (m.text or "").strip()
    photo_file_id: Optional[str] = None

    if raw.lower() == "нет":
        photo_file_id = None
    elif m.photo:
        photo_file_id = m.photo[-1].file_id
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        photo_file_id = m.document.file_id
    else:
        return await m.answer("Не вижу фото 😅 Пришли фото или напиши `нет`.")

    await state.update_data(photo_file_id=photo_file_id)

    if photo_file_id and caption_too_long(text):
        await state.set_state(CreatePost.long_with_photo_choice)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Короткий caption + текст отдельно", callback_data="longphoto:split")],
            [InlineKeyboardButton(text="📝 Без фото (весь текст одним сообщением)", callback_data="longphoto:nophoto")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="draft:cancel")],
        ])
        return await m.answer(
            f"Текст слишком длинный для подписи к фото (лимит ~{CAPTION_LIMIT}).\n"
            "Как поступаем?",
            reply_markup=kb
        )

    await show_preview_create(m, state, text=text, buttons=buttons, photo_file_id=photo_file_id, split_text=False)

@dp.callback_query(F.data.startswith("longphoto:"))
async def cb_longphoto_choice(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])
    photo_file_id = data.get("photo_file_id")

    if c.data == "longphoto:nophoto":
        await state.update_data(photo_file_id=None, split_text=False)
        await state.set_state(CreatePost.preview)
        await c.message.answer("🧾 Предпросмотр поста (без фото):")
        await c.message.answer(text, reply_markup=build_kb(buttons))
        await c.message.answer("Что делаем дальше?", reply_markup=preview_actions_kb())
        await c.answer()
        return

    if c.data == "longphoto:split":
        await state.update_data(split_text=True)
        await state.set_state(CreatePost.preview)
        short_caption = (text[:CAPTION_LIMIT - 3] + "…") if len(text) > CAPTION_LIMIT else text

        await c.message.answer("🧾 Предпросмотр поста (фото + текст отдельным сообщением):")
        await c.message.answer_photo(photo_file_id, caption=short_caption, reply_markup=None)
        await c.message.answer(text, reply_markup=build_kb(buttons))
        await c.message.answer("Что делаем дальше?", reply_markup=preview_actions_kb())
        await c.answer()
        return

    await c.answer()

async def show_preview_create(m: Message, state: FSMContext, text: str, buttons: list, photo_file_id: Optional[str], split_text: bool):
    await state.update_data(split_text=split_text)
    await state.set_state(CreatePost.preview)

    await m.answer("🧾 Предпросмотр поста:")
    if photo_file_id:
        if split_text:
            caption = (text[:CAPTION_LIMIT - 3] + "…") if len(text) > CAPTION_LIMIT else text
            await m.answer_photo(photo_file_id, caption=caption, reply_markup=None)
            await m.answer(text, reply_markup=build_kb(buttons))
        else:
            await m.answer_photo(photo_file_id, caption=text, reply_markup=build_kb(buttons))
    else:
        await m.answer(text, reply_markup=build_kb(buttons))

    await m.answer("Что делаем дальше?", reply_markup=preview_actions_kb())

@dp.callback_query(F.data == "draft:cancel")
async def cb_draft_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.edit_text("Ок, отменено.")
    except Exception:
        await c.message.answer("Ок, отменено.")
    await c.answer()


# ---------- PUBLISH / STORE ----------
async def publish(
    bot: Bot,
    channel_id: str,
    text: str,
    buttons: list,
    created_by: int,
    photo_file_id: Optional[str],
    split_text: bool,
) -> str:
    """
    Returns post_id. Stores record in DB.
    If photo_file_id and split_text=True -> send photo (short caption) + send text message with buttons.
    buttons live on the text message in split_text mode.
    """
    assert POOL is not None

    buttons_json = json.dumps(buttons, ensure_ascii=False)
    text_msg_id: Optional[int] = None

    if photo_file_id:
        if split_text:
            short_caption = (text[:CAPTION_LIMIT - 3] + "…") if len(text) > CAPTION_LIMIT else text
            photo_msg = await bot.send_photo(channel_id, photo_file_id, caption=short_caption, reply_markup=None)
            text_msg = await bot.send_message(channel_id, text, reply_markup=build_kb(buttons))
            main_message_id = photo_msg.message_id
            text_msg_id = text_msg.message_id
        else:
            photo_msg = await bot.send_photo(channel_id, photo_file_id, caption=text, reply_markup=build_kb(buttons))
            main_message_id = photo_msg.message_id
    else:
        msg = await bot.send_message(channel_id, text, reply_markup=build_kb(buttons))
        main_message_id = msg.message_id

    post_id = f"{int(now_tz().timestamp())}_{created_by}_{main_message_id}"

    async with POOL.acquire() as conn:
        await conn.execute("""
            INSERT INTO posts (id, channel_id, message_id, text_msg_id, text, buttons_json, photo_file_id, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, post_id, channel_id, main_message_id, text_msg_id, text, buttons_json, photo_file_id, created_by)

    return post_id


# ---------- CALLBACKS (publish now / schedule) ----------
@dp.callback_query(F.data == "draft:pub_now")
async def cb_pub_now(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    if not CHANNEL_ID:
        await c.answer("Не задан CHANNEL_ID (Render → Environment).", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])
    photo_file_id = data.get("photo_file_id")
    split_text = bool(data.get("split_text", False))

    try:
        post_id = await publish(bot, CHANNEL_ID, text, buttons, c.from_user.id, photo_file_id, split_text)
    except Exception as e:
        await c.answer("Не смог опубликовать. Проверь права бота в канале.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    await state.clear()
    try:
        await c.message.edit_text("✅ Опубликовано!")
    except Exception:
        await c.message.answer("✅ Опубликовано!")

    await c.message.answer(
        f"Управление постом (id: `{post_id}`):",
        parse_mode="Markdown",
        reply_markup=post_controls_kb(post_id),
    )
    await c.answer()

@dp.callback_query(F.data == "draft:schedule")
async def cb_schedule_start(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await c.message.answer(f"Выбери время публикации ({tz_label()}):", reply_markup=quick_times_kb("draft_time", "draft"))
    await c.answer()

@dp.callback_query(F.data.startswith("draft_time:draft:"))
async def cb_draft_time(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    code = c.data.split(":", 2)[2]
    if code == "manual":
        await state.set_state(CreatePost.schedule_manual)
        await c.message.answer(
            "Введи дату и время в формате:\n"
            "`DD.MM.YYYY HH:MM`\n"
            f"Например: `{now_tz().strftime('%d.%m.%Y %H:%M')}`",
            parse_mode="Markdown"
        )
        return await c.answer()

    run_at = calc_quick_dt(code)
    await state.update_data(run_at_iso=run_at.isoformat())
    await finalize_schedule(c.message, state)

@dp.message(CreatePost.schedule_manual)
async def draft_manual_dt(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    s = (m.text or "").strip()
    try:
        run_at = parse_dt_local(s)
    except Exception:
        return await m.answer("Не понял формат 😅 Пример: `15.01.2026 12:00`", parse_mode="Markdown")

    await state.update_data(run_at_iso=run_at.isoformat())
    await finalize_schedule(m, state)

async def finalize_schedule(target: Message, state: FSMContext):
    assert POOL is not None

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])
    photo_file_id = data.get("photo_file_id")
    run_at_iso = data.get("run_at_iso")

    if not CHANNEL_ID:
        await state.clear()
        return await target.answer("Не задан CHANNEL_ID (Render → Environment).")

    run_at = datetime.fromisoformat(run_at_iso)
    if run_at <= now_tz() + timedelta(seconds=30):
        return await target.answer("Время должно быть хотя бы на 1 минуту позже текущего.")

    job_id = f"{int(now_tz().timestamp())}_{target.from_user.id}"
    buttons_json = json.dumps(buttons, ensure_ascii=False)

    async with POOL.acquire() as conn:
        await conn.execute("""
            INSERT INTO jobs (id, channel_id, text, buttons_json, photo_file_id, run_at, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, job_id, CHANNEL_ID, text, buttons_json, photo_file_id, run_at, target.from_user.id)

    await state.clear()
    await target.answer(f"✅ Запланировано на {fmt_dt(run_at)} ({tz_label()})")
    await target.answer(
        f"Управление запланированным (id: `{job_id}`):",
        parse_mode="Markdown",
        reply_markup=job_controls_kb(job_id),
    )


# ---------- JOBS ----------
@dp.message(F.text == BTN_JOBS)
async def menu_jobs(m: Message):
    await cmd_jobs(m)

@dp.message(Command("jobs"))
async def cmd_jobs(m: Message):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    async with POOL.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, run_at
            FROM jobs
            ORDER BY run_at ASC
            LIMIT 20
        """)

    if not rows:
        return await m.answer("Запланированных постов нет.", reply_markup=admin_menu_kb(is_owner(m.from_user.id)))

    await m.answer("📅 Запланированные (последние 20):")
    for r in rows:
        job_id = r["id"]
        dt = r["run_at"]
        short = (r["text"] or "").strip().replace("\n", " ")
        if len(short) > 60:
            short = short[:60] + "…"
        await m.answer(
            f"⏰ {fmt_dt(dt)} ({tz_label()})\n🆔 `{job_id}`\n📝 {short}",
            parse_mode="Markdown",
            reply_markup=job_controls_kb(job_id),
        )

@dp.callback_query(F.data.startswith("job:view:"))
async def cb_job_view(c: CallbackQuery):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    job_id = c.data.split(":", 2)[2]
    async with POOL.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
    if not r:
        await c.answer("Не нашла задачу.", show_alert=True)
        return

    dt = r["run_at"]
    buttons = json.loads(r["buttons_json"])
    photo_file_id = r["photo_file_id"]
    text = r["text"]

    await c.message.answer(f"👁 Запланировано на: {fmt_dt(dt)} ({tz_label()})\n🆔 `{job_id}`", parse_mode="Markdown")
    if photo_file_id:
        if caption_too_long(text):
            short_caption = (text[:CAPTION_LIMIT - 3] + "…") if len(text) > CAPTION_LIMIT else text
            await c.message.answer_photo(photo_file_id, caption=short_caption, reply_markup=None)
            await c.message.answer(text, reply_markup=build_kb(buttons))
        else:
            await c.message.answer_photo(photo_file_id, caption=text, reply_markup=build_kb(buttons))
    else:
        await c.message.answer(text, reply_markup=build_kb(buttons))

    await c.answer()

# ---- NEW: EDIT JOB (content) ----
@dp.callback_query(F.data.startswith("job:edit:"))
async def cb_job_edit_start(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    job_id = c.data.split(":", 2)[2]
    async with POOL.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
    if not r:
        await c.answer("Не нашла задачу.", show_alert=True)
        return

    await state.clear()
    await state.set_state(EditJob.text)
    await state.update_data(edit_job_id=job_id)

    await c.message.answer("✏️ Редактирование отложки: пришли НОВЫЙ текст поста.")
    await c.answer()

@dp.message(EditJob.text)
async def editjob_get_text(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    text = (m.text or "").strip()
    if not text:
        return await m.answer("Нужен текст.")
    await state.update_data(new_text=text)
    await state.set_state(EditJob.buttons)
    await m.answer(
        "Теперь НОВЫЕ кнопки (по одной строке):\n"
        "Текст - https://example.com\n\n"
        "Если кнопки не нужны — напиши `нет`",
        parse_mode="Markdown"
    )

@dp.message(EditJob.buttons)
async def editjob_get_buttons(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    raw = (m.text or "").strip()
    buttons = [] if raw.lower() == "нет" else parse_buttons(raw)
    await state.update_data(new_buttons=buttons)

    await state.set_state(EditJob.photo)
    await m.answer(
        "Теперь пришли НОВОЕ фото (если хочешь заменить).\n"
        "Если оставить старое фото — напиши `оставить`.\n"
        "Если убрать фото — напиши `убрать`.",
        parse_mode="Markdown"
    )

@dp.message(EditJob.photo)
async def editjob_get_photo(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    data = await state.get_data()
    job_id = data.get("edit_job_id")
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])

    async with POOL.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
    if not r:
        await state.clear()
        return await m.answer("Не нашла задачу.")

    incoming = (m.text or "").strip().lower()
    photo_file_id: Optional[str] = None

    if m.photo:
        photo_file_id = m.photo[-1].file_id
        await state.update_data(photo_file_id=photo_file_id)
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        photo_file_id = m.document.file_id
        await state.update_data(photo_file_id=photo_file_id)
    elif incoming == "оставить":
        await state.update_data(photo_file_id=r["photo_file_id"])
    elif incoming == "убрать":
        await state.update_data(photo_file_id=None)
    else:
        return await m.answer("Не вижу фото 😅 Пришли фото или напиши `оставить` / `убрать`.")

    photo_file_id = (await state.get_data()).get("photo_file_id")

    if photo_file_id and caption_too_long(new_text):
        await state.set_state(EditJob.long_with_photo_choice)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Короткий caption + текст отдельно", callback_data="editjoblong:split")],
            [InlineKeyboardButton(text="📝 Без фото (весь текст одним сообщением)", callback_data="editjoblong:nophoto")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="draft:cancel")],
        ])
        return await m.answer(
            f"Текст слишком длинный для подписи к фото (лимит ~{CAPTION_LIMIT}).\n"
            "Как поступаем?",
            reply_markup=kb
        )

    await show_preview_editjob(m, state, new_text, new_buttons, photo_file_id, split_text=False)

@dp.callback_query(F.data.startswith("editjoblong:"))
async def cb_editjoblong_choice(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    data = await state.get_data()
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])
    photo_file_id = data.get("photo_file_id")

    if c.data == "editjoblong:nophoto":
        await state.update_data(photo_file_id=None, split_text=False)
        await show_preview_editjob(c.message, state, new_text, new_buttons, None, split_text=False)
        await c.answer()
        return

    if c.data == "editjoblong:split":
        await state.update_data(split_text=True)
        await show_preview_editjob(c.message, state, new_text, new_buttons, photo_file_id, split_text=True)
        await c.answer()
        return

    await c.answer()

async def show_preview_editjob(target: Message, state: FSMContext, text: str, buttons: list, photo_file_id: Optional[str], split_text: bool):
    await state.update_data(split_text=split_text)
    await state.set_state(EditJob.preview)

    await target.answer("🧾 Предпросмотр обновлённой отложки:")
    if photo_file_id:
        if split_text:
            short_caption = (text[:CAPTION_LIMIT - 3] + "…") if len(text) > CAPTION_LIMIT else text
            await target.answer_photo(photo_file_id, caption=short_caption, reply_markup=None)
            await target.answer(text, reply_markup=build_kb(buttons))
        else:
            await target.answer_photo(photo_file_id, caption=text, reply_markup=build_kb(buttons))
    else:
        await target.answer(text, reply_markup=build_kb(buttons))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить изменения", callback_data="job:apply_edit")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="draft:cancel")],
    ])
    await target.answer("Сохранить изменения в отложке?", reply_markup=kb)

@dp.callback_query(F.data == "job:apply_edit")
async def cb_job_apply_edit(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    data = await state.get_data()
    job_id = data.get("edit_job_id")
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])
    photo_file_id = data.get("photo_file_id")

    buttons_json = json.dumps(new_buttons, ensure_ascii=False)

    async with POOL.acquire() as conn:
        r = await conn.fetchrow("SELECT id FROM jobs WHERE id=$1", job_id)
    if not r:
        await c.answer("Не нашла задачу.", show_alert=True)
        await state.clear()
        return

    async with POOL.acquire() as conn:
        await conn.execute("""
            UPDATE jobs
            SET text=$1, buttons_json=$2, photo_file_id=$3
            WHERE id=$4
        """, new_text, buttons_json, photo_file_id, job_id)

    await state.clear()
    await c.message.answer("✅ Обновила отложенный пост. Время публикации осталось прежним.", reply_markup=job_controls_kb(job_id))
    await c.answer()

# ---- delete job ----
@dp.callback_query(F.data.startswith("job:del:"))
async def cb_job_del_ask(c: CallbackQuery):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    job_id = c.data.split(":", 2)[2]
    await c.message.answer("Подтвердить удаление?", reply_markup=job_delete_confirm_kb(job_id))
    await c.answer()

@dp.callback_query(F.data.startswith("job:del_no:"))
async def cb_job_del_no(c: CallbackQuery):
    await c.message.edit_text("Ок, не удаляю.")
    await c.answer()

@dp.callback_query(F.data.startswith("job:del_yes:"))
async def cb_job_del_yes(c: CallbackQuery):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    job_id = c.data.split(":", 2)[2]
    async with POOL.acquire() as conn:
        res = await conn.execute("DELETE FROM jobs WHERE id=$1", job_id)

    if res.startswith("DELETE 1"):
        await c.message.edit_text("✅ Удалила запланированный пост.")
    else:
        await c.message.edit_text("Не нашла задачу (возможно, уже отправлена).")
    await c.answer()

# ---- move job time ----
@dp.callback_query(F.data.startswith("job:move:"))
async def cb_job_move_start(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    job_id = c.data.split(":", 2)[2]
    await state.clear()
    await state.update_data(move_job_id=job_id)
    await c.message.answer(f"Выбери новое время ({tz_label()}):", reply_markup=quick_times_kb("job_time", job_id))
    await c.answer()

@dp.callback_query(F.data.startswith("job_time:"))
async def cb_job_time_pick(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    _, job_id, code = c.data.split(":", 2)

    if code == "manual":
        await state.set_state(MoveJob.manual)
        await state.update_data(move_job_id=job_id)
        await c.message.answer(
            "Введи дату и время в формате:\n"
            "`DD.MM.YYYY HH:MM`\n"
            f"Например: `{now_tz().strftime('%d.%m.%Y %H:%M')}`",
            parse_mode="Markdown"
        )
        return await c.answer()

    new_dt = calc_quick_dt(code)
    async with POOL.acquire() as conn:
        res = await conn.execute("UPDATE jobs SET run_at=$1 WHERE id=$2", new_dt, job_id)

    await state.clear()
    if res.startswith("UPDATE 1"):
        await c.message.answer(f"✅ Перенесла на {fmt_dt(new_dt)} ({tz_label()})")
    else:
        await c.message.answer("Не нашла задачу.")
    await c.answer()

@dp.message(MoveJob.manual)
async def job_move_manual(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    data = await state.get_data()
    job_id = data.get("move_job_id")
    if not job_id:
        await state.clear()
        return await m.answer("Не нашла задачу.")

    s = (m.text or "").strip()
    try:
        new_dt = parse_dt_local(s)
    except Exception:
        return await m.answer("Не понял формат 😅 Пример: `15.01.2026 12:00`", parse_mode="Markdown")

    async with POOL.acquire() as conn:
        res = await conn.execute("UPDATE jobs SET run_at=$1 WHERE id=$2", new_dt, job_id)

    await state.clear()
    if res.startswith("UPDATE 1"):
        await m.answer(f"✅ Перенесла на {fmt_dt(new_dt)} ({tz_label()})")
    else:
        await m.answer("Не нашла задачу.")


# ---------- POSTS (LIST / EDIT / DELETE) ----------
@dp.message(F.text == BTN_POSTS)
async def menu_posts(m: Message):
    await cmd_posts(m)

@dp.message(Command("posts"))
async def cmd_posts(m: Message):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    async with POOL.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, text, created_at
            FROM posts
            ORDER BY created_at DESC
            LIMIT 10
        """)

    if not rows:
        return await m.answer("Пока нет постов, опубликованных ботом.")

    await m.answer("🧾 Последние 10 опубликованных ботом:")
    for r in rows:
        post_id = r["id"]
        dt = r["created_at"]
        short = (r["text"] or "").strip().replace("\n", " ")
        if len(short) > 60:
            short = short[:60] + "…"
        await m.answer(
            f"🕒 {fmt_dt(dt)} ({tz_label()})\n🆔 `{post_id}`\n📝 {short}",
            parse_mode="Markdown",
            reply_markup=post_controls_kb(post_id)
        )

@dp.callback_query(F.data.startswith("post:del:"))
async def cb_post_del_ask(c: CallbackQuery):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    post_id = c.data.split(":", 2)[2]
    await c.message.answer("Подтвердить удаление?", reply_markup=post_delete_confirm_kb(post_id))
    await c.answer()

@dp.callback_query(F.data.startswith("post:del_no:"))
async def cb_post_del_no(c: CallbackQuery):
    await c.message.edit_text("Ок, не удаляю.")
    await c.answer()

@dp.callback_query(F.data.startswith("post:del_yes:"))
async def cb_post_del_yes(c: CallbackQuery, bot: Bot):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    post_id = c.data.split(":", 2)[2]
    async with POOL.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM posts WHERE id=$1", post_id)

    if not p:
        await c.answer("Пост не найден.", show_alert=True)
        return

    try:
        await bot.delete_message(chat_id=p["channel_id"], message_id=p["message_id"])
    except Exception as e:
        await c.answer("Не смогла удалить. Проверь права бота.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    if p["text_msg_id"]:
        try:
            await bot.delete_message(chat_id=p["channel_id"], message_id=p["text_msg_id"])
        except Exception:
            pass

    async with POOL.acquire() as conn:
        await conn.execute("DELETE FROM posts WHERE id=$1", post_id)

    await c.message.edit_text("✅ Удалила пост из канала.")
    await c.answer()

@dp.callback_query(F.data.startswith("post:edit:"))
async def cb_post_edit_start(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    post_id = c.data.split(":", 2)[2]
    assert POOL is not None
    async with POOL.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM posts WHERE id=$1", post_id)
    if not p:
        await c.answer("Пост не найден.", show_alert=True)
        return

    await state.clear()
    await state.set_state(EditPost.text)
    await state.update_data(edit_post_id=post_id)
    await c.message.answer("✏️ Редактирование: пришли НОВЫЙ текст поста.")
    await c.answer()

@dp.message(EditPost.text)
async def edit_get_text(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    text = (m.text or "").strip()
    if not text:
        return await m.answer("Нужен текст.")
    await state.update_data(new_text=text)
    await state.set_state(EditPost.buttons)
    await m.answer(
        "Теперь НОВЫЕ кнопки (по одной строке):\n"
        "Текст - https://example.com\n\n"
        "Если кнопки не нужны — напиши `нет`",
        parse_mode="Markdown"
    )

@dp.message(EditPost.buttons)
async def edit_get_buttons(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    raw = (m.text or "").strip()
    buttons = [] if raw.lower() == "нет" else parse_buttons(raw)
    await state.update_data(new_buttons=buttons)

    await state.set_state(EditPost.photo)
    await m.answer(
        "Теперь пришли НОВОЕ фото (если хочешь заменить).\n"
        "Если оставить старое фото — напиши `оставить`.\n"
        "Если убрать фото — напиши `убрать`.",
        parse_mode="Markdown"
    )

@dp.message(EditPost.photo)
async def edit_get_photo(m: Message, state: FSMContext):
    if not await db_is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    assert POOL is not None

    data = await state.get_data()
    post_id = data.get("edit_post_id")
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])

    async with POOL.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM posts WHERE id=$1", post_id)
    if not p:
        await state.clear()
        return await m.answer("Пост не найден.")

    incoming = (m.text or "").strip().lower()
    photo_file_id: Optional[str] = None

    if m.photo:
        photo_file_id = m.photo[-1].file_id
        await state.update_data(photo_file_id=photo_file_id)
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        photo_file_id = m.document.file_id
        await state.update_data(photo_file_id=photo_file_id)
    elif incoming == "оставить":
        await state.update_data(photo_file_id=p["photo_file_id"])
    elif incoming == "убрать":
        await state.update_data(photo_file_id=None)
    else:
        return await m.answer("Не вижу фото 😅 Пришли фото или напиши `оставить` / `убрать`.")

    photo_file_id = (await state.get_data()).get("photo_file_id")

    if photo_file_id and caption_too_long(new_text):
        await state.set_state(EditPost.long_with_photo_choice)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Короткий caption + текст отдельно", callback_data="editlong:split")],
            [InlineKeyboardButton(text="📝 Без фото (весь текст одним сообщением)", callback_data="editlong:nophoto")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="draft:cancel")],
        ])
        return await m.answer(
            f"Текст слишком длинный для подписи к фото (лимит ~{CAPTION_LIMIT}).\n"
            "Как поступаем?",
            reply_markup=kb
        )

    await show_preview_edit(m, state, new_text, new_buttons, photo_file_id, split_text=False)

@dp.callback_query(F.data.startswith("editlong:"))
async def cb_editlong_choice(c: CallbackQuery, state: FSMContext):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    data = await state.get_data()
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])
    photo_file_id = data.get("photo_file_id")

    if c.data == "editlong:nophoto":
        await state.update_data(photo_file_id=None, split_text=False)
        await show_preview_edit(c.message, state, new_text, new_buttons, None, split_text=False)
        await c.answer()
        return

    if c.data == "editlong:split":
        await state.update_data(split_text=True)
        await show_preview_edit(c.message, state, new_text, new_buttons, photo_file_id, split_text=True)
        await c.answer()
        return

    await c.answer()

async def show_preview_edit(target: Message, state: FSMContext, text: str, buttons: list, photo_file_id: Optional[str], split_text: bool):
    await state.update_data(split_text=split_text)
    await state.set_state(EditPost.preview)

    await target.answer("🧾 Предпросмотр обновлённого поста:")
    if photo_file_id:
        if split_text:
            short_caption = (text[:CAPTION_LIMIT - 3] + "…") if len(text) > CAPTION_LIMIT else text
            await target.answer_photo(photo_file_id, caption=short_caption, reply_markup=None)
            await target.answer(text, reply_markup=build_kb(buttons))
        else:
            await target.answer_photo(photo_file_id, caption=text, reply_markup=build_kb(buttons))
    else:
        await target.answer(text, reply_markup=build_kb(buttons))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить изменения", callback_data="post:apply_edit")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="draft:cancel")],
    ])
    await target.answer("Применить изменения?", reply_markup=kb)

@dp.callback_query(F.data == "post:apply_edit")
async def cb_post_apply_edit(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not await db_is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    assert POOL is not None

    data = await state.get_data()
    post_id = data.get("edit_post_id")
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])
    photo_file_id = data.get("photo_file_id")
    split_text = bool(data.get("split_text", False))

    async with POOL.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM posts WHERE id=$1", post_id)
    if not p:
        await c.answer("Пост не найден.", show_alert=True)
        return

    buttons_kb = build_kb(new_buttons)
    buttons_json = json.dumps(new_buttons, ensure_ascii=False)

    try:
        if not photo_file_id:
            if p["text_msg_id"]:
                try:
                    await bot.delete_message(chat_id=p["channel_id"], message_id=p["text_msg_id"])
                except Exception:
                    pass

            await bot.edit_message_text(
                chat_id=p["channel_id"],
                message_id=p["message_id"],
                text=new_text,
                reply_markup=buttons_kb,
            )

            async with POOL.acquire() as conn:
                await conn.execute("""
                    UPDATE posts
                    SET text=$1, buttons_json=$2, photo_file_id=NULL, text_msg_id=NULL
                    WHERE id=$3
                """, new_text, buttons_json, post_id)

        else:
            if split_text:
                short_caption = (new_text[:CAPTION_LIMIT - 3] + "…") if len(new_text) > CAPTION_LIMIT else new_text

                try:
                    await bot.edit_message_caption(
                        chat_id=p["channel_id"],
                        message_id=p["message_id"],
                        caption=short_caption,
                        reply_markup=None
                    )
                except Exception:
                    try:
                        await bot.delete_message(chat_id=p["channel_id"], message_id=p["message_id"])
                    except Exception:
                        pass
                    photo_msg = await bot.send_photo(p["channel_id"], photo_file_id, caption=short_caption, reply_markup=None)
                    async with POOL.acquire() as conn:
                        await conn.execute("UPDATE posts SET message_id=$1 WHERE id=$2", photo_msg.message_id, post_id)

                if p["text_msg_id"]:
                    try:
                        await bot.edit_message_text(
                            chat_id=p["channel_id"],
                            message_id=p["text_msg_id"],
                            text=new_text,
                            reply_markup=buttons_kb,
                        )
                    except Exception:
                        try:
                            await bot.delete_message(chat_id=p["channel_id"], message_id=p["text_msg_id"])
                        except Exception:
                            pass
                        txt_msg = await bot.send_message(p["channel_id"], new_text, reply_markup=buttons_kb)
                        async with POOL.acquire() as conn:
                            await conn.execute("UPDATE posts SET text_msg_id=$1 WHERE id=$2", txt_msg.message_id, post_id)
                else:
                    txt_msg = await bot.send_message(p["channel_id"], new_text, reply_markup=buttons_kb)
                    async with POOL.acquire() as conn:
                        await conn.execute("UPDATE posts SET text_msg_id=$1 WHERE id=$2", txt_msg.message_id, post_id)

                async with POOL.acquire() as conn:
                    await conn.execute("""
                        UPDATE posts
                        SET text=$1, buttons_json=$2, photo_file_id=$3
                        WHERE id=$4
                    """, new_text, buttons_json, photo_file_id, post_id)

            else:
                if p["text_msg_id"]:
                    try:
                        await bot.delete_message(chat_id=p["channel_id"], message_id=p["text_msg_id"])
                    except Exception:
                        pass

                try:
                    await bot.edit_message_caption(
                        chat_id=p["channel_id"],
                        message_id=p["message_id"],
                        caption=new_text,
                        reply_markup=buttons_kb
                    )
                except Exception:
                    try:
                        await bot.delete_message(chat_id=p["channel_id"], message_id=p["message_id"])
                    except Exception:
                        pass
                    photo_msg = await bot.send_photo(p["channel_id"], photo_file_id, caption=new_text, reply_markup=buttons_kb)
                    async with POOL.acquire() as conn:
                        await conn.execute("UPDATE posts SET message_id=$1 WHERE id=$2", photo_msg.message_id, post_id)

                async with POOL.acquire() as conn:
                    await conn.execute("""
                        UPDATE posts
                        SET text=$1, buttons_json=$2, photo_file_id=$3, text_msg_id=NULL
                        WHERE id=$4
                    """, new_text, buttons_json, photo_file_id, post_id)

    except Exception as e:
        await c.answer("Не смогла применить изменения. Проверь права бота.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    await state.clear()
    await c.message.answer("✅ Обновила пост в канале.", reply_markup=post_controls_kb(post_id))
    await c.answer()


# ================== SCHEDULER ==================
async def scheduler_loop(bot: Bot):
    assert POOL is not None
    while True:
        try:
            now = datetime.now(tz=TZ)

            async with POOL.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, channel_id, text, buttons_json, photo_file_id, created_by, run_at
                    FROM jobs
                    WHERE run_at <= $1
                    ORDER BY run_at ASC
                    LIMIT 20
                """, now)

            for r in rows:
                job_id = r["id"]
                try:
                    buttons = json.loads(r["buttons_json"])
                    text = r["text"]
                    photo_file_id = r["photo_file_id"]

                    split_text = bool(photo_file_id and caption_too_long(text))

                    await publish(
                        bot,
                        r["channel_id"],
                        text,
                        buttons,
                        int(r["created_by"]),
                        photo_file_id,
                        split_text,
                    )

                    async with POOL.acquire() as conn:
                        await conn.execute("DELETE FROM jobs WHERE id=$1", job_id)

                except Exception as e:
                    print(f"[scheduler] job_id={job_id} failed: {e}")

        except Exception as e:
            print(f"[scheduler] loop error: {e}")

        await asyncio.sleep(20)


# ================== WEB SERVER ==================
async def run_web_server():
    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render → Environment.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty. Set it in Render → Environment.")

    await init_db()

    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    await run_web_server()
    asyncio.create_task(scheduler_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

