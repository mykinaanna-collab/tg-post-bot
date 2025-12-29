import os
import json
import asyncio
from dataclasses import dataclass, asdict
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


# ================== ENV (Render → Environment Variables) ==================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip())

# Для канала лучше @username (надёжнее), но можно и -100...
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()

TIMEZONE = (os.getenv("TIMEZONE") or "Europe/Moscow").strip()
TZ = ZoneInfo(TIMEZONE)

# Старые админы из ENV (можно оставить пустым, если полностью перейдёшь на /addadmin)
ENV_ADMINS = set(
    int(x.strip()) for x in (os.getenv("ADMIN_IDS", "") or "").split(",") if x.strip().isdigit()
)

# Файлы хранения
ADMINS_FILE = "admins.json"
JOBS_FILE = "jobs.json"
POSTS_FILE = "posts.json"


# ================== UI (ADMIN MENU) ==================
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

def parse_buttons(text: str):
    """
    Формат строк:
    Текст - https://example.com
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
    """
    Формат: DD.MM.YYYY HH:MM (МСК)
    """
    dt = datetime.strptime(s.strip(), "%d.%m.%Y %H:%M")
    return dt.replace(tzinfo=TZ)

def quick_times_kb(prefix: str, entity_id: str) -> InlineKeyboardMarkup:
    """
    prefix:
      - draft_time (для черновика)
      - job_time (для переноса job)
    callback:
      f"{prefix}:{entity_id}:today12" и т.п.
    """
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


# ================== ADMIN STORAGE ==================
def load_admins() -> set[int]:
    s: set[int] = set()
    if OWNER_ID:
        s.add(OWNER_ID)

    if not os.path.exists(ADMINS_FILE):
        return s

    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for x in raw:
            if isinstance(x, int):
                s.add(x)
            elif isinstance(x, str) and x.strip().isdigit():
                s.add(int(x.strip()))
    except Exception:
        pass

    return s

def save_admins(admins: set[int]) -> None:
    admins = set(admins)
    if OWNER_ID:
        admins.add(OWNER_ID)
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(admins)), f, ensure_ascii=False, indent=2)

ADMIN_IDS = load_admins() | ENV_ADMINS
save_admins(ADMIN_IDS)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ================== JOBS STORAGE ==================
@dataclass
class Job:
    id: str
    channel_id: str
    text: str
    buttons: list
    run_at_iso: str
    created_by: int

def load_jobs() -> list[Job]:
    if not os.path.exists(JOBS_FILE):
        return []
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [Job(**item) for item in raw]
    except Exception:
        return []

def save_jobs(jobs: list[Job]) -> None:
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(j) for j in jobs], f, ensure_ascii=False, indent=2)

JOBS: list[Job] = load_jobs()

def find_job(job_id: str) -> Optional[Job]:
    for j in JOBS:
        if j.id == job_id:
            return j
    return None


# ================== POSTS STORAGE ==================
@dataclass
class PublishedPost:
    id: str
    channel_id: str
    message_id: int
    text: str
    buttons: list
    created_by: int
    created_at_iso: str

def load_posts() -> list[PublishedPost]:
    if not os.path.exists(POSTS_FILE):
        return []
    try:
        with open(POSTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [PublishedPost(**item) for item in raw]
    except Exception:
        return []

def save_posts(posts: list[PublishedPost]) -> None:
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in posts], f, ensure_ascii=False, indent=2)

POSTS: list[PublishedPost] = load_posts()

def find_post(post_id: str) -> Optional[PublishedPost]:
    for p in POSTS:
        if p.id == post_id:
            return p
    return None


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
            InlineKeyboardButton(text="✏️ Перенести", callback_data=f"job:move:{job_id}"),
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
    preview = State()
    schedule_manual = State()

class EditPost(StatesGroup):
    text = State()
    buttons = State()
    preview = State()

class MoveJob(StatesGroup):
    manual = State()


# ================== BOT ==================
dp = Dispatcher()


# ---------- COMMON ----------
@dp.message(Command("start"))
async def start(m: Message):
    uid = m.from_user.id
    if is_admin(uid):
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
    if not is_admin(uid):
        return await m.answer("Меню доступно только админам.")
    await m.answer("Меню 👇", reply_markup=admin_menu_kb(is_owner(uid)))

@dp.message(Command("myid"))
async def myid(m: Message):
    uid = m.from_user.id
    await m.answer(
        "Диагностика:\n"
        f"- твой user_id: {uid}\n"
        f"- ты админ по мнению бота: {is_admin(uid)}\n"
        f"- TIMEZONE: {TIMEZONE}\n"
        f"- CHANNEL_ID: {CHANNEL_ID!r}\n"
    )

@dp.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    if is_admin(m.from_user.id):
        await m.answer("Ок, отменено.", reply_markup=admin_menu_kb(is_owner(m.from_user.id)))
    else:
        await m.answer("Ок, отменено.", reply_markup=ReplyKeyboardRemove())


# ---------- MENU BUTTONS (admins only) ----------
@dp.message(F.text == BTN_MYID)
async def menu_myid(m: Message):
    await myid(m)

@dp.message(F.text == BTN_CANCEL)
async def menu_cancel(m: Message, state: FSMContext):
    await cancel_cmd(m, state)

@dp.message(F.text == BTN_HELP)
async def menu_help(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Доступ к функциям — только админам.")
    await m.answer(
        "Что умею:\n"
        "• 📝 Новый пост (текст + кнопки)\n"
        "• 📅 Запланированные (посмотреть/перенести/удалить)\n"
        "• 🧾 Опубликованные (редактировать/удалить)\n\n"
        "Если меню пропало — /menu",
        reply_markup=admin_menu_kb(is_owner(m.from_user.id))
    )


# ---------- ADMIN MGMT (OWNER) ----------
@dp.message(F.text == BTN_ADMINS)
async def menu_admins(m: Message):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")
    await m.answer("Админы:\n" + "\n".join(str(x) for x in sorted(ADMIN_IDS)))

@dp.message(Command("admins"))
async def cmd_admins(m: Message):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")
    await m.answer("Админы:\n" + "\n".join(str(x) for x in sorted(ADMIN_IDS)))

@dp.message(Command("addadmin"))
async def cmd_addadmin(m: Message):
    if not is_owner(m.from_user.id):
        return await m.answer("Нет доступа.")

    # Вариант 1: /addadmin 123456789
    parts = (m.text or "").split()
    if len(parts) == 2 and parts[1].isdigit():
        uid = int(parts[1])
        ADMIN_IDS.add(uid)
        save_admins(ADMIN_IDS)
        return await m.answer(f"✅ Добавила админа: {uid}")

    # Вариант 2: /addadmin как reply на пересланное сообщение
    if m.reply_to_message:
        # ВАЖНО: при пересылке from_user = ты, а настоящий автор в forward_from (если не скрыт)
        if m.reply_to_message.forward_from:
            uid = m.reply_to_message.forward_from.id
            ADMIN_IDS.add(uid)
            save_admins(ADMIN_IDS)
            return await m.answer(f"✅ Добавила админа по пересланному сообщению: {uid}")

        # Если forward_from пуст — у человека включена privacy пересылки
        # Тогда безопасно добавлять по from_user нельзя (это будет твой id)
        if m.reply_to_message.from_user and m.reply_to_message.from_user.id == m.from_user.id:
            return await m.answer(
                "Не могу определить сотрудника по пересылке — Telegram скрывает автора (privacy).\n\n"
                "Варианты:\n"
                "1) Пусть сотрудник напишет боту /myid и пришлёт тебе цифры → /addadmin 123\n"
                "2) Или сотрудник может временно разрешить показывать автора при пересылке."
            )

        # Если это не пересылка, а ты ответила на сообщение другого человека (теоретически),
        # тогда можно взять from_user
        if m.reply_to_message.from_user:
            uid = m.reply_to_message.from_user.id
            if uid == m.from_user.id:
                return await m.answer("Похоже, это твоё сообщение 🙂 Пришли /addadmin 123 или ответь на сообщение сотрудника.")
            ADMIN_IDS.add(uid)
            save_admins(ADMIN_IDS)
            return await m.answer(f"✅ Добавила админа по сообщению: {uid}")

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
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Использование: /deladmin 123456789")
    uid = int(parts[1])
    if uid == OWNER_ID:
        return await m.answer("OWNER удалить нельзя 🙂")
    if uid in ADMIN_IDS:
        ADMIN_IDS.remove(uid)
        save_admins(ADMIN_IDS)
        await m.answer(f"✅ Удалила админа: {uid}")
    else:
        await m.answer("Такого админа нет.")


# ---------- CREATE POST ----------
@dp.message(F.text == BTN_NEWPOST)
async def menu_newpost(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    await state.clear()
    await state.set_state(CreatePost.text)
    await m.answer("Пришли текст поста.")

@dp.message(Command("newpost"))
async def cmd_newpost(m: Message, state: FSMContext):
    await menu_newpost(m, state)

@dp.message(CreatePost.text)
async def create_get_text(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
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
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    data = await state.get_data()
    text = data["text"]

    raw = (m.text or "").strip()
    if raw.lower() == "нет":
        buttons = []
    else:
        buttons = parse_buttons(raw)

    await state.update_data(buttons=buttons)
    await state.set_state(CreatePost.preview)

    await m.answer("🧾 Предпросмотр поста:")
    await m.answer(text, reply_markup=build_kb(buttons))
    await m.answer("Что делаем дальше?", reply_markup=preview_actions_kb())

@dp.callback_query(F.data == "draft:cancel")
async def cb_draft_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Ок, отменено.")
    await c.answer()

async def publish(bot: Bot, channel_id: str, text: str, buttons: list, created_by: int) -> PublishedPost:
    msg = await bot.send_message(channel_id, text, reply_markup=build_kb(buttons))
    post_id = f"{int(now_tz().timestamp())}_{created_by}_{msg.message_id}"
    p = PublishedPost(
        id=post_id,
        channel_id=channel_id,
        message_id=msg.message_id,
        text=text,
        buttons=buttons,
        created_by=created_by,
        created_at_iso=now_tz().isoformat(),
    )
    POSTS.append(p)
    save_posts(POSTS)
    return p

@dp.callback_query(F.data == "draft:pub_now")
async def cb_pub_now(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    if not CHANNEL_ID:
        await c.answer("Не задан CHANNEL_ID (Render → Environment).", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])

    try:
        p = await publish(bot, CHANNEL_ID, text, buttons, c.from_user.id)
    except Exception as e:
        await c.answer("Не смог опубликовать. Проверь права бота в канале.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    await state.clear()
    await c.message.edit_text("✅ Опубликовано!")
    await c.message.answer(
        f"Управление постом (id: `{p.id}`):",
        parse_mode="Markdown",
        reply_markup=post_controls_kb(p.id),
    )
    await c.answer()

@dp.callback_query(F.data == "draft:schedule")
async def cb_schedule_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await c.message.answer("Выбери время публикации (МСК):", reply_markup=quick_times_kb("draft_time", "draft"))
    await c.answer()

@dp.callback_query(F.data.startswith("draft_time:draft:"))
async def cb_draft_time(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
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
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    s = (m.text or "").strip()
    try:
        run_at = parse_dt_local(s)
    except Exception:
        return await m.answer("Не понял формат 😅 Пример: `15.01.2026 12:00`", parse_mode="Markdown")

    await state.update_data(run_at_iso=run_at.isoformat())
    await finalize_schedule(m, state)

async def finalize_schedule(target: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])
    run_at_iso = data.get("run_at_iso")

    if not CHANNEL_ID:
        await state.clear()
        return await target.answer("Не задан CHANNEL_ID (Render → Environment).")

    run_at = datetime.fromisoformat(run_at_iso)
    if run_at <= now_tz() + timedelta(seconds=30):
        return await target.answer("Время должно быть хотя бы на 1 минуту позже текущего.")

    job_id = f"{int(now_tz().timestamp())}_{target.from_user.id}"
    job = Job(
        id=job_id,
        channel_id=CHANNEL_ID,
        text=text,
        buttons=buttons,
        run_at_iso=run_at.isoformat(),
        created_by=target.from_user.id,
    )
    JOBS.append(job)
    save_jobs(JOBS)
    await state.clear()
    await target.answer(f"✅ Запланировано на {fmt_dt(run_at)} (МСК)")
    await target.answer(f"Управление запланированным (id: `{job.id}`):", parse_mode="Markdown", reply_markup=job_controls_kb(job.id))


# ---------- JOBS LIST / VIEW / MOVE / DELETE ----------
@dp.message(F.text == BTN_JOBS)
async def menu_jobs(m: Message):
    await cmd_jobs(m)

@dp.message(Command("jobs"))
async def cmd_jobs(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    if not JOBS:
        return await m.answer("Запланированных постов нет.", reply_markup=admin_menu_kb(is_owner(m.from_user.id)))

    items = sorted(JOBS, key=lambda j: j.run_at_iso)[:20]
    await m.answer("📅 Запланированные (последние 20):")
    for j in items:
        dt = datetime.fromisoformat(j.run_at_iso)
        short = (j.text or "").strip().replace("\n", " ")
        if len(short) > 60:
            short = short[:60] + "…"
        await m.answer(
            f"⏰ {fmt_dt(dt)}\n🆔 `{j.id}`\n📝 {short}",
            parse_mode="Markdown",
            reply_markup=job_controls_kb(j.id)
        )

@dp.callback_query(F.data.startswith("job:view:"))
async def cb_job_view(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    job_id = c.data.split(":", 2)[2]
    j = find_job(job_id)
    if not j:
        await c.answer("Не нашла задачу.", show_alert=True)
        return

    dt = datetime.fromisoformat(j.run_at_iso)
    await c.message.answer(f"👁 Запланировано на: {fmt_dt(dt)} (МСК)\n🆔 `{j.id}`", parse_mode="Markdown")
    await c.message.answer(j.text, reply_markup=build_kb(j.buttons))
    await c.answer()

@dp.callback_query(F.data.startswith("job:del:"))
async def cb_job_del_ask(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    job_id = c.data.split(":", 2)[2]
    if not find_job(job_id):
        await c.answer("Не нашла задачу.", show_alert=True)
        return
    await c.message.answer("Подтвердить удаление?", reply_markup=job_delete_confirm_kb(job_id))
    await c.answer()

@dp.callback_query(F.data.startswith("job:del_no:"))
async def cb_job_del_no(c: CallbackQuery):
    await c.message.edit_text("Ок, не удаляю.")
    await c.answer()

@dp.callback_query(F.data.startswith("job:del_yes:"))
async def cb_job_del_yes(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    job_id = c.data.split(":", 2)[2]
    before = len(JOBS)
    JOBS[:] = [x for x in JOBS if x.id != job_id]
    if len(JOBS) == before:
        await c.answer("Не нашла задачу.", show_alert=True)
        return
    save_jobs(JOBS)
    await c.message.edit_text("✅ Удалила запланированный пост.")
    await c.answer()

@dp.callback_query(F.data.startswith("job:move:"))
async def cb_job_move_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    job_id = c.data.split(":", 2)[2]
    j = find_job(job_id)
    if not j:
        await c.answer("Не нашла задачу.", show_alert=True)
        return
    await state.clear()
    await state.update_data(move_job_id=job_id)
    await c.message.answer("Выбери новое время (МСК):", reply_markup=quick_times_kb("job_time", job_id))
    await c.answer()

@dp.callback_query(F.data.startswith("job_time:"))
async def cb_job_time_pick(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    _, job_id, code = c.data.split(":", 2)
    j = find_job(job_id)
    if not j:
        await c.answer("Не нашла задачу.", show_alert=True)
        return

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
    j.run_at_iso = new_dt.isoformat()
    save_jobs(JOBS)
    await state.clear()
    await c.message.answer(f"✅ Перенесла на {fmt_dt(new_dt)} (МСК)")
    await c.answer()

@dp.message(MoveJob.manual)
async def job_move_manual(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    data = await state.get_data()
    job_id = data.get("move_job_id")
    j = find_job(job_id) if job_id else None
    if not j:
        await state.clear()
        return await m.answer("Не нашла задачу.")

    s = (m.text or "").strip()
    try:
        new_dt = parse_dt_local(s)
    except Exception:
        return await m.answer("Не понял формат 😅 Пример: `15.01.2026 12:00`", parse_mode="Markdown")

    j.run_at_iso = new_dt.isoformat()
    save_jobs(JOBS)
    await state.clear()
    await m.answer(f"✅ Перенесла на {fmt_dt(new_dt)} (МСК)")


# ---------- POSTS LIST / EDIT / DELETE ----------
@dp.message(F.text == BTN_POSTS)
async def menu_posts(m: Message):
    await cmd_posts(m)

@dp.message(Command("posts"))
async def cmd_posts(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    if not POSTS:
        return await m.answer("Пока нет постов, опубликованных ботом.")

    recent = sorted(POSTS, key=lambda p: p.created_at_iso)[-10:]
    await m.answer("🧾 Последние 10 опубликованных ботом:")
    for p in reversed(recent):
        dt = datetime.fromisoformat(p.created_at_iso)
        short = (p.text or "").strip().replace("\n", " ")
        if len(short) > 60:
            short = short[:60] + "…"
        await m.answer(
            f"🕒 {fmt_dt(dt)}\n🆔 `{p.id}`\n📝 {short}",
            parse_mode="Markdown",
            reply_markup=post_controls_kb(p.id)
        )

@dp.callback_query(F.data.startswith("post:del:"))
async def cb_post_del_ask(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    post_id = c.data.split(":", 2)[2]
    if not find_post(post_id):
        await c.answer("Пост не найден.", show_alert=True)
        return
    await c.message.answer("Подтвердить удаление?", reply_markup=post_delete_confirm_kb(post_id))
    await c.answer()

@dp.callback_query(F.data.startswith("post:del_no:"))
async def cb_post_del_no(c: CallbackQuery):
    await c.message.edit_text("Ок, не удаляю.")
    await c.answer()

@dp.callback_query(F.data.startswith("post:del_yes:"))
async def cb_post_del_yes(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    post_id = c.data.split(":", 2)[2]
    p = find_post(post_id)
    if not p:
        await c.answer("Пост не найден.", show_alert=True)
        return

    try:
        await bot.delete_message(chat_id=p.channel_id, message_id=p.message_id)
    except Exception as e:
        await c.answer("Не смогла удалить. Проверь права бота.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    POSTS[:] = [x for x in POSTS if x.id != post_id]
    save_posts(POSTS)
    await c.message.edit_text("✅ Удалила пост из канала.")
    await c.answer()

@dp.callback_query(F.data.startswith("post:edit:"))
async def cb_post_edit_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    post_id = c.data.split(":", 2)[2]
    p = find_post(post_id)
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
    if not is_admin(m.from_user.id):
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
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    raw = (m.text or "").strip()
    if raw.lower() == "нет":
        buttons = []
    else:
        buttons = parse_buttons(raw)

    data = await state.get_data()
    post_id = data.get("edit_post_id")
    new_text = data.get("new_text", "")

    await state.update_data(new_buttons=buttons)
    await state.set_state(EditPost.preview)

    await m.answer("🧾 Предпросмотр обновлённого поста:")
    await m.answer(new_text, reply_markup=build_kb(buttons))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить изменения", callback_data=f"post:apply_edit:{post_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="draft:cancel")],
    ])
    await m.answer("Применить изменения?", reply_markup=kb)

@dp.callback_query(F.data.startswith("post:apply_edit:"))
async def cb_post_apply_edit(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    post_id = c.data.split(":", 2)[2]
    p = find_post(post_id)
    if not p:
        await c.answer("Пост не найден.", show_alert=True)
        return

    data = await state.get_data()
    new_text = data.get("new_text", "")
    new_buttons = data.get("new_buttons", [])

    try:
        await bot.edit_message_text(
            chat_id=p.channel_id,
            message_id=p.message_id,
            text=new_text,
            reply_markup=build_kb(new_buttons),
        )
    except Exception as e:
        await c.answer("Не смогла отредактировать. Проверь права бота.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    p.text = new_text
    p.buttons = new_buttons
    save_posts(POSTS)

    await state.clear()
    await c.message.answer("✅ Обновила пост в канале.", reply_markup=post_controls_kb(p.id))
    await c.answer()


# ================== SCHEDULER ==================
async def scheduler_loop(bot: Bot):
    while True:
        try:
            due = []
            n = now_tz()
            for j in JOBS:
                dt = datetime.fromisoformat(j.run_at_iso)
                if dt <= n:
                    due.append(j)

            if due:
                for j in due:
                    try:
                        _ = await publish(bot, j.channel_id, j.text, j.buttons, j.created_by)
                    except Exception:
                        continue
                    JOBS.remove(j)
                save_jobs(JOBS)

        except Exception:
            pass

        await asyncio.sleep(20)


# ================== WEB SERVER (Render port binding) ==================
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

    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)

    await run_web_server()
    asyncio.create_task(scheduler_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


