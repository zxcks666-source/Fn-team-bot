# language: python, file: bot.py
# Fortnite Team Finder. aiogram 3.x + aiohttp + SQLite.
# Версия для Render (Webhook)

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "fn_teams.db")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render подставит сам

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fnbot")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var required")

# ---------- OPTIONS ----------

MODES = {
    "build": "🏗 BR Build",
    "zerobuild": "🌿 BR Zero Build",
    "creative": "🎨 Creative",
    "ranked": "🏆 Ranked",
    "stw": "🧟 Save the World",
    "any": "🌐 Любой",
}
STYLES = {
    "casual": "😌 Casual",
    "competitive": "🔥 Competitive",
    "any": "🌐 Любой",
}
REGIONS = {
    "eu": "🇪🇺 EU",
    "na": "🇺🇸 NA",
    "cis": "🇷🇺 CIS",
    "asia": "🌏 Asia",
    "any": "🌐 Любой",
}
RANKS = {
    "any": "🌐 Любой",
    "bronze": "🥉 Bronze",
    "silver": "🥈 Silver",
    "gold": "🥇 Gold",
    "plat": "💎 Platinum",
    "diamond": "💠 Diamond",
    "elite": "👑 Elite+",
}

def now_utc():
    return datetime.now(timezone.utc)

# ---------- DB ----------

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            mode TEXT DEFAULT 'any',
            mic INTEGER DEFAULT 0,
            voice INTEGER DEFAULT 0,
            style TEXT DEFAULT 'any',
            region TEXT DEFAULT 'any',
            rank TEXT DEFAULT 'any',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS queue (
            user_id INTEGER PRIMARY KEY,
            party_total INTEGER,
            joined_at TEXT
        );
    """)
    con.commit()
    con.close()

db_init()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def get_user(uid):
    con = db()
    r = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return dict(r) if r else None

def ensure_user(uid, username):
    if not get_user(uid):
        con = db()
        con.execute(
            "INSERT INTO users (user_id, username, updated_at) VALUES (?, ?, ?)",
            (uid, username or "", now_utc().isoformat()),
        )
        con.commit()
        con.close()

def update_field(uid, field, value):
    allowed = {"mode", "mic", "voice", "style", "region", "rank"}
    if field not in allowed:
        return
    con = db()
    con.execute(f"UPDATE users SET {field}=?, updated_at=? WHERE user_id=?",
                (value, now_utc().isoformat(), uid))
    con.commit()
    con.close()

# ---------- QUEUE ----------

def get_queue(party_total, exclude=None):
    con = db()
    q = """SELECT q.user_id, q.party_total, q.joined_at,
                  u.username, u.mode, u.mic, u.voice, u.style, u.region, u.rank
           FROM queue q JOIN users u ON u.user_id=q.user_id
           WHERE q.party_total=?"""
    params = [party_total]
    if exclude:
        q += " AND q.user_id != ?"
        params.append(exclude)
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]

def enqueue(uid, party_total):
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO queue (user_id, party_total, joined_at) VALUES (?, ?, ?)",
        (uid, party_total, now_utc().isoformat()),
    )
    con.commit()
    con.close()

def dequeue(uid):
    con = db()
    con.execute("DELETE FROM queue WHERE user_id=?", (uid,))
    con.commit()
    con.close()

def is_queued(uid):
    con = db()
    r = con.execute("SELECT party_total FROM queue WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return r[0] if r else None

def all_queued():
    con = db()
    rows = con.execute("SELECT user_id, party_total FROM queue").fetchall()
    con.close()
    return [dict(r) for r in rows]

# ---------- MATCHING ----------

def filters_match(a, b):
    for key in ("mode", "style", "region", "rank"):
        va, vb = a[key], b[key]
        if va != "any" and vb != "any" and va != vb:
            return False
    for key in ("mic", "voice"):
        if bool(a[key]) != bool(b[key]):
            return False
    return True

def find_team(uid):
    me = get_user(uid)
    if not me:
        return None
    party_total = is_queued(uid)
    if not party_total:
        return None
    pool = get_queue(party_total, exclude=uid)
    compatible = [u for u in pool if filters_match(me, u)]
    if len(compatible) < party_total - 1:
        return None
    compatible.sort(key=lambda u: u["joined_at"])
    return compatible[:party_total - 1]

# ---------- KEYBOARDS ----------

def main_menu_kb(queued=None):
    rows = [
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
    ]
    if queued:
        rows.append([InlineKeyboardButton(text="❌ Покинуть очередь", callback_data="leave")])
    else:
        rows.append([InlineKeyboardButton(text="🔍 Найти команду", callback_data="find")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def profile_kb(user):
    rows = [
        [InlineKeyboardButton(text=f"Режим: {MODES[user['mode']]}", callback_data="set:mode")],
        [InlineKeyboardButton(text=f"Ранг: {RANKS[user['rank']]}", callback_data="set:rank")],
        [InlineKeyboardButton(text=f"Регион: {REGIONS[user['region']]}", callback_data="set:region")],
        [InlineKeyboardButton(text=f"Стиль: {STYLES[user['style']]}", callback_data="set:style")],
        [InlineKeyboardButton(text=f"🎤 Микрофон: {'да' if user['mic'] else 'нет'}", callback_data="toggle:mic")],
        [InlineKeyboardButton(text=f"🔊 Голосовой чат: {'да' if user['voice'] else 'нет'}", callback_data="toggle:voice")],
        [InlineKeyboardButton(text="◀ В меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def choice_kb(field, options, current):
    rows = []
    for key, label in options.items():
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"pick:{field}:{key}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="profile")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def team_size_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 1 человек — дуо", callback_data="party:2")],
        [InlineKeyboardButton(text="👥👥 2 человека — трио", callback_data="party:3")],
        [InlineKeyboardButton(text="👥👥👥 3 человека — сквад", callback_data="party:4")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="menu")],
    ])

# ---------- BOT ----------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def fmt_profile(user):
    mic = "🎤 да" if user["mic"] else "🔇 нет"
    voice = "🔊 да" if user["voice"] else "🔕 нет"
    return (
        f"<b>Твой профиль</b>\n\n"
        f"Режим: {MODES[user['mode']]}\n"
        f"Ранг: {RANKS[user['rank']]}\n"
        f"Регион: {REGIONS[user['region']]}\n"
        f"Стиль: {STYLES[user['style']]}\n"
        f"Микрофон: {mic}\n"
        f"Голосовой чат: {voice}\n\n"
        f"<i>Если у обоих в поле «Любой» — подходит. Если оба указали разное — не матч. Микрофон и голос должны совпадать.</i>"
    )

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    ensure_user(msg.from_user.id, msg.from_user.username)
    queued = is_queued(msg.from_user.id)
    await msg.answer(
        "<b>Fortnite Team Finder</b> 🎮\n\n"
        "Настрой профиль под себя — режим, ранг, регион, стиль, микрофон, голосовой чат.\n"
        "Потом жми <b>«Найти команду»</b>, выбирай размер — дуо / трио / сквад — и я подберу тиммейтов с такими же интересами.\n\n"
        "Матч происходит в реальном времени: как только появляется совместимый игрок — оба получаете уведомление с контактом.",
        reply_markup=main_menu_kb(queued),
    )

@dp.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.username)
    queued = is_queued(cb.from_user.id)
    await cb.message.edit_text("Главное меню. Что делаем?", reply_markup=main_menu_kb(queued))
    await cb.answer()

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.username)
    user = get_user(cb.from_user.id)
    await cb.message.edit_text(fmt_profile(user), reply_markup=profile_kb(user))
    await cb.answer()

@dp.callback_query(F.data.startswith("set:"))
async def cb_set_field(cb: CallbackQuery):
    field = cb.data.split(":")[1]
    user = get_user(cb.from_user.id)
    options = {"mode": MODES, "rank": RANKS, "region": REGIONS, "style": STYLES}.get(field)
    if not options:
        await cb.answer("Неизвестное поле")
        return
    await cb.message.edit_text(
        f"Выбери {field}:",
        reply_markup=choice_kb(field, options, user[field]),
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("pick:"))
async def cb_pick(cb: CallbackQuery):
    _, field, value = cb.data.split(":")
    update_field(cb.from_user.id, field, value)
    user = get_user(cb.from_user.id)
    await cb.message.edit_text(fmt_profile(user), reply_markup=profile_kb(user))
    await cb.answer("Сохранено")

@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(cb: CallbackQuery):
    field = cb.data.split(":")[1]
    user = get_user(cb.from_user.id)
    update_field(cb.from_user.id, field, 0 if user[field] else 1)
    user = get_user(cb.from_user.id)
    await cb.message.edit_text(fmt_profile(user), reply_markup=profile_kb(user))
    await cb.answer("Обновлено")

@dp.callback_query(F.data == "find")
async def cb_find(cb: CallbackQuery):
    ensure_user(cb.from_user.id, cb.from_user.username)
    queued = is_queued(cb.from_user.id)
    if queued:
        await cb.message.edit_text(
            f"Ты уже в очереди — ищешь команду на {queued} человек.\n"
            f"Жди, пришлю уведомление когда найдётся совместимый тиммейт.",
            reply_markup=main_menu_kb(queued),
        )
        await cb.answer()
        return
    await cb.message.edit_text("Сколько человек в команде ищешь?", reply_markup=team_size_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("party:"))
async def cb_party(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(uid, cb.from_user.username)

    party_total = int(cb.data.split(":")[1])
    enqueue(uid, party_total)
    log.info("enqueue user=%s party=%s", uid, party_total)

    teammates = find_team(uid)
    if teammates:
        await form_team(uid, party_total, teammates)
    else:
        await cb.message.edit_text(
            f"⏳ Ты в очереди. Ищу {party_total - 1} тиммейт{'а' if party_total == 2 else 'ов'} под твои фильтры.\n\n"
            f"Как только кто-то подходящий зайдёт — пришлю уведомление с контактом.",
            reply_markup=main_menu_kb(party_total),
        )
    await cb.answer()

async def form_team(uid, party_total, teammates):
    all_ids = [uid] + [t["user_id"] for t in teammates]

    for i in all_ids:
        dequeue(i)

    con = db()
    names = {}
    for i in all_ids:
        row = con.execute("SELECT username FROM users WHERE user_id=?", (i,)).fetchone()
        names[i] = row["username"] if row and row["username"] else None
    con.close()

    for member_id in all_ids:
        others = [i for i in all_ids if i != member_id]
        rendered = []
        for oid in others:
            if names.get(oid):
                rendered.append(f"@{names[oid]}")
            else:
                rendered.append(f"<a href='tg://user?id={oid}'>игрок_{oid}</a>")
        text = (
            "🎉 <b>Команда найдена!</b>\n\n"
            f"Твой тиммейт{'ы' if len(others) > 1 else ''}: {', '.join(rendered)}\n\n"
            f"Напиши{'те' if len(others) > 1 else ''} и договоритесь об игре.\n"
            f"Если что-то не сложилось — жми «Найти команду» снова."
        )
        try:
            await bot.send_message(member_id, text, reply_markup=main_menu_kb())
        except Exception as e:
            log.warning("notify failed %s: %s", member_id, e)

    log.info("team formed: %s", all_ids)

@dp.callback_query(F.data == "leave")
async def cb_leave(cb: CallbackQuery):
    dequeue(cb.from_user.id)
    await cb.message.edit_text("Ты вышел из очереди.", reply_markup=main_menu_kb())
    await cb.answer("Вышел")

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "Команды:\n"
        "/start — меню\n"
        "/help — справка\n\n"
        "Как работает матч:\n"
        "• Оба указали разные режимы / регионы / ранги / стили — не подходит.\n"
        "• У одного «Любой» — подходит под любого.\n"
        "• Микрофон и голосовой чат должны совпадать у обоих.\n"
        "• Матч ищется в реальном времени — как только появляется пара, оба получают уведомление."
    )

# ---------- WEBHOOK & BACKGROUND TASKS ----------

async def queue_scanner():
    """Периодическая проверка очереди на случай упущенных матчей."""
    while True:
        await asyncio.sleep(20)
        try:
            queued = all_queued()
            processed = set()
            for row in queued:
                uid = row["user_id"]
                if uid in processed:
                    continue
                teammates = find_team(uid)
                if teammates:
                    await form_team(uid, row["party_total"], teammates)
                    processed.add(uid)
                    for t in teammates:
                        processed.add(t["user_id"])
        except Exception as e:
            log.error("scanner error: %s", e)

async def on_startup(bot: Bot):
    # Запускаем фоновый сканер
    asyncio.create_task(queue_scanner())
    
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        log.info(f"Webhook set to {webhook_url}")
    else:
        log.info("No RENDER_EXTERNAL_URL, starting polling...")
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(dp.start_polling(bot))

def main():
    app = web.Application()
    # Регистрируем вебхук
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
    # Вешаем событие старта
    app.on_startup.append(on_startup)

    port = int(os.getenv("PORT", 8080))
    log.info(f"Starting web server on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
