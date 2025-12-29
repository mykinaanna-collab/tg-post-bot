import os
import json
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
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


# ================== ENV (Render → Environment Variables) ==================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip())

CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()  # лучше @username канала
TIMEZONE = (os.getenv("TIMEZONE") or "Europe/Moscow").strip()
TZ = ZoneInfo(TIMEZONE)

# Старые админы из ENV (можно оставить пустым после перехода на /addadmin)
ENV_ADMINS = set(
    int(x.strip()) for x in (os.getenv("ADMIN_IDS", "") or "").split(",") if x.strip().isdigit()
)

# Файлы хранения
JOBS_FILE = "jobs.json"
ADMINS_FILE = "admins.json"


# ================== HELPERS ==================
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

        # самые частые разделители
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
    rows = []
    for title, url in buttons:
        rows.append([InlineKeyboardButton(text=title, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview_actions_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать сейчас", callback_data="pub_now")],
        [InlineKeyboardButton(text="📅 Запланировать", callback_data="schedule")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])


def parse_dt_local(s: str) -> datetime:
    """
    Формат: DD.MM.YYYY HH:MM
    """
    dt = datetime.strptime(s.strip(), "%d.%m.%Y %H:%M")
    return dt.replace(tzinfo=TZ)


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
        admins.add(OWNER_ID)  # OWNER нельзя потерять
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(admins)), f, ensure_ascii=False, indent=2)


ADMIN_IDS = load_admins() | ENV_ADMINS
save_admins(ADMIN_IDS)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ================== JOB STORAGE ==================
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


# ================== FSM ==================
class Post(StatesGroup):
    text = State()
    buttons = State()
    preview = State()
    schedule_dt = State()


# ================== BOT ==================
dp = Dispatcher()


@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Привет! Я бот для публикации постов в канал с кнопками.\n\n"
        "Команды:\n"
        "/newpost — создать пост\n"
        "/myid — узнать свой user_id\n"
        "/jobs — список запланированных (для админов)\n"
        "/deljob ID — удалить запланированный (для админов)\n\n"
        "Админы (только для владельца):\n"
        "/admins — список\n"
        "/addadmin 123 — добавить по id\n"
        "/addadmin (в ответ на пересланное сообщение) — добавить по пересланному\n"
        "/deladmin 123 — удалить\n\n"
        f"Таймзона: {TIMEZONE}"
    )


@dp.message(Command("myid"))
async def myid(m: Message):
    uid = m.from_user.id
    await m.answer(
        "Диагностика:\n"
        f"- твой user_id: {uid}\n"
        f"- ты админ по мнению бота: {is_admin(uid)}\n"
        f"- OWNER_ID: {OWNER_ID}\n"
        f"- TIMEZONE: {TIMEZONE}\n"
        f"- CHANNEL_ID: {CHANNEL_ID!r}\n"
    )


# --------- ADMIN COMMANDS (OWNER ONLY) ---------
@dp.message(Command("admins"))
async def cmd_admins(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Нет доступа.")
    await m.answer("Админы:\n" + "\n".join(str(x) for x in sorted(ADMIN_IDS)))


@dp.message(Command("addadmin"))
async def cmd_addadmin(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Нет доступа.")

    # Вариант 1: /addadmin 123456789
    parts = (m.text or "").split()
    if len(parts) == 2 and parts[1].isdigit():
        uid = int(parts[1])
        ADMIN_IDS.add(uid)
        save_admins(ADMIN_IDS)
        return await m.answer(f"✅ Добавила админа: {uid}")

    # Вариант 2: /addadmin как reply на пересланное сообщение
    # (или reply на любое сообщение человека)
    if m.reply_to_message and m.reply_to_message.from_user:
        uid = m.reply_to_message.from_user.id
        ADMIN_IDS.add(uid)
        save_admins(ADMIN_IDS)
        return await m.answer(f"✅ Добавила админа по сообщению: {uid}")

    # Если не получилось
    await m.answer(
        "Как добавить админа:\n"
        "1) /addadmin 123456789\n"
        "или\n"
        "2) Перешли сообщение сотрудника → ответь на него командой /addadmin"
    )


@dp.message(Command("deladmin"))
async def cmd_deladmin(m: Message):
    if m.from_user.id != OWNER_ID:
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


# --------- POST FLOW ---------
@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Ок, отменено.")


@dp.message(Command("newpost"))
async def newpost(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    await state.set_state(Post.text)
    await m.answer("Пришли текст поста.")


@dp.message(Post.text)
async def get_text(m: Message, state: FSMContext):
    text = (m.text or "").strip()
    if not text:
        return await m.answer("Нужен текст поста.")
    await state.update_data(text=text)
    await state.set_state(Post.buttons)
    await m.answer(
        "Теперь кнопки (по одной строке):\n"
        "Текст - https://example.com\n\n"
        "Если кнопки не нужны — напиши `нет`",
        parse_mode="Markdown"
    )


@dp.message(Post.buttons)
async def get_buttons(m: Message, state: FSMContext):
    data = await state.get_data()
    text = data["text"]

    raw = (m.text or "").strip()
    if raw.lower() == "нет":
        buttons = []
    else:
        buttons = parse_buttons(raw)

    await state.update_data(buttons=buttons)
    await state.set_state(Post.preview)

    kb = build_kb(buttons)
    await m.answer("🧾 Предпросмотр поста:")
    await m.answer(text, reply_markup=kb)
    await m.answer("Что делаем дальше?", reply_markup=preview_actions_kb())


@dp.callback_query(F.data == "cancel")
async def cb_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Ок, отменено.")
    await c.answer()


@dp.callback_query(F.data == "pub_now")
async def cb_pub_now(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    if not CHANNEL_ID:
        await c.answer("Не задан CHANNEL_ID в Render → Environment.", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])
    kb = build_kb(buttons)

    try:
        await bot.send_message(CHANNEL_ID, text, reply_markup=kb)
    except Exception as e:
        await c.answer("Не смог опубликовать. Проверь права бота в канале.", show_alert=True)
        await c.message.answer(f"Ошибка: {e}")
        return

    await state.clear()
    await c.message.edit_text(f"✅ Опубликовано! Куда: {CHANNEL_ID!r}")
    await c.answer()


@dp.callback_query(F.data == "schedule")
async def cb_schedule(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(Post.schedule_dt)
    now = datetime.now(TZ)
    await c.message.answer(
        "Ок, запланируем.\n"
        "Введи дату и время в формате:\n"
        "`DD.MM.YYYY HH:MM`\n"
        f"Например: `{now.strftime('%d.%m.%Y %H:%M')}`\n\n"
        f"Таймзона: *{TIMEZONE}*",
        parse_mode="Markdown"
    )
    await c.answer()


@dp.message(Post.schedule_dt)
async def set_schedule_dt(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    if not CHANNEL_ID:
        return await m.answer("Не задан CHANNEL_ID в Render → Environment.")

    s = (m.text or "").strip()
    try:
        run_at = parse_dt_local(s)
    except Exception:
        return await m.answer("Не понял дату/время 😅\nФормат: `29.12.2025 18:30`", parse_mode="Markdown")

    now = datetime.now(TZ)
    if run_at <= now + timedelta(seconds=30):
        return await m.answer("Время должно быть хотя бы на 1 минуту позже текущего.")

    data = await state.get_data()
    text = data.get("text", "")
    buttons = data.get("buttons", [])

    job_id = f"{int(now.timestamp())}_{m.from_user.id}"
    job = Job(
        id=job_id,
        channel_id=CHANNEL_ID,
        text=text,
        buttons=buttons,
        run_at_iso=run_at.isoformat(),
        created_by=m.from_user.id,
    )
    JOBS.append(job)
    save_jobs(JOBS)

    await state.clear()
    await m.answer(f"✅ Запланировано на {run_at.strftime('%d.%m.%Y %H:%M')} ({TIMEZONE})")


@dp.message(Command("jobs"))
async def list_jobs(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    if not JOBS:
        return await m.answer("Запланированных постов нет.")
    lines = ["📅 Запланированные посты:"]
    for j in sorted(JOBS, key=lambda x: x.run_at_iso):
        dt = datetime.fromisoformat(j.run_at_iso)
        lines.append(f"- {dt.strftime('%d.%m.%Y %H:%M')} — id: `{j.id}`")
    lines.append("\nУдалить: /deljob ID")
    await m.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("deljob"))
async def del_job(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Нет доступа.")
    parts = (m.text or "").split()
    if len(parts) != 2:
        return await m.answer("Использование: /deljob ID")
    jid = parts[1].strip()
    before = len(JOBS)
    JOBS[:] = [j for j in JOBS if j.id != jid]
    if len(JOBS) == before:
        return await m.answer("Не нашла такую задачу.")
    save_jobs(JOBS)
    await m.answer("✅ Удалила задачу.")


# ================== SCHEDULER ==================
async def scheduler_loop(bot: Bot):
    while True:
        try:
            now = datetime.now(TZ)
            due = []
            for j in JOBS:
                dt = datetime.fromisoformat(j.run_at_iso)
                if dt <= now:
                    due.append(j)

            if due:
                for j in due:
                    try:
                        kb = build_kb(j.buttons)
                        await bot.send_message(j.channel_id, j.text, reply_markup=kb)
                    except Exception:
                        # если не отправилось — оставляем, чтобы не потерять
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

    # На всякий: если когда-то включали webhook
    await bot.delete_webhook(drop_pending_updates=True)

    # Render требует открытый порт
    await run_web_server()

    # Планировщик отложенных постов
    asyncio.create_task(scheduler_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
