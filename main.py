import os
import json
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


# ===== НАСТРОЙКИ (Render → Environment) =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Канал: лучше chat_id вида -100...
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Админы (user_id через запятую): "111,222"
ADMIN_IDS = set(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

# Таймзона (можно не трогать). По умолчанию +07:00
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bangkok")
TZ = ZoneInfo(TIMEZONE)

# Файл для запланированных постов
JOBS_FILE = "jobs.json"


# ===== УТИЛИТЫ =====
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

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
        # допускаем разные тире
        for sep in [" - ", " — ", " – ", "-"]:
            if sep in line:
                left, right = line.split(sep, 1)
                title = left.strip()
                url = right.strip()
                if title and url.startswith(("http://", "https://")):
                    buttons.append((title[:64], url))
                break
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
    Ожидаем формат: DD.MM.YYYY HH:MM
    Пример: 29.12.2025 18:30
    """
    s = s.strip()
    dt = datetime.strptime(s, "%d.%m.%Y %H:%M")
    return dt.replace(tzinfo=TZ)


# ===== ХРАНЕНИЕ ЗАДАЧ =====
@dataclass
class Job:
    id: str
    channel_id: str
    text: str
    buttons: list
    run_at_iso: str  # ISO datetime with tz
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


# ===== СОСТОЯНИЯ =====
class Post(StatesGroup):
    text = State()
    buttons = State()
    preview = State()
    schedule_dt = State()


# ===== БОТ =====
dp = Dispatcher()

@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "Привет! Я бот для публикации постов в канал с кнопками.\n\n"
        "Команды:\n"
        "/newpost — создать пост\n"
        "/myid — узнать свой user_id\n"
        "/cancel — отменить текущий шаг\n"
        "/jobs — список запланированных (для админов)\n\n"
        "Формат кнопок:\n"
        "Текст - https://ссылка"
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
    await m.answer("Пришли текст поста (обычное сообщение).")

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
        "Если кнопки не нужны — напиши `нет`"
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
    await c.message.edit_text("✅ Опубликовано!")
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
        f"Например: `{now.strftime('%d.%m.%Y %H:%M')}`",
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
    lines.append("\nЧтобы удалить: /deljob ID")
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


# ===== ФОНОВЫЙ ПЛАНИРОВЩИК =====
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
                        # если не отправилось — оставим, чтобы не потерять
                        continue
                    # если отправилось — удаляем
                    JOBS.remove(j)
                save_jobs(JOBS)

        except Exception:
            pass

        await asyncio.sleep(20)  # проверка каждые 20 секунд


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Render → Environment.")
    bot = Bot(BOT_TOKEN)

    # Запускаем планировщик
    asyncio.create_task(scheduler_loop(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
