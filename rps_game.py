# -*- coding: utf-8 -*-
"""
بازی سنگ‌کاغذقیچی - ماژول کاملاً جدا، مثل dooz_game.py کنار bot.py قرار می‌گیره.
=================================================================================
نکته‌ی طراحیِ مهم (چطوری جواب لو نمی‌ره):
تو حالتِ «بازی با کاربر»، هر دو بازیکن رو یه پیامِ مشترکِ داخل گروه دکمه می‌زنن. وقتی
یکی‌شون یه گزینه (سنگ/کاغذ/قیچی) رو می‌زنه، بازی‌اش تو دیتابیس ذخیره می‌شه ولی خودِ
پیامِ گروه دست‌نخورده می‌مونه - فقط با یه پاپ‌آپِ خصوصی (show_alert=True که تلگرام فقط
به همون کاربر نشون می‌ده، نه بقیه‌ی گروه) بهش می‌گیم «ثبت شد». وقتی هر دو نفر انتخاب
کردن، تازه پیامِ گروه ویرایش می‌شه و هر دو انتخاب + برنده اعلام می‌شه.
"""
import os
import json
import random
from datetime import datetime

import game_router
import pg_compat

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

DB_PATH = os.environ.get("CHATR_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatr_bot.db"))

import sqlite3


class _PersistentConnProxy:
    """کانکشن رو یه‌بار باز نگه می‌داره؛ close() عمداً بی‌اثره تا هیچ‌جای فایل نیاز به تغییر نداشته باشه."""
    __slots__ = ("_real",)

    def __init__(self, real_conn):
        object.__setattr__(self, "_real", real_conn)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


_DB_CONN = None


def get_conn():
    global _DB_CONN
    if _DB_CONN is None:
        pg_url = pg_compat.postgres_url()
        if pg_url:
            _DB_CONN = pg_compat.connect(pg_url)
        else:
            real = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
            real.row_factory = sqlite3.Row
            real.execute("PRAGMA journal_mode=WAL")
            real.execute("PRAGMA busy_timeout=8000")
            _DB_CONN = _PersistentConnProxy(real)
    return _DB_CONN


def _ensure_schema():
    conn = get_conn()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS rps_games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            player1_id INTEGER NOT NULL,
            player1_name TEXT,
            player2_id INTEGER,
            player2_name TEXT,
            choice1 TEXT,
            choice2 TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            message_id INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS rps_player_stats (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            total_games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()


_ensure_schema()

CHOICES = ("rock", "paper", "scissors")
CHOICE_LABEL = {"rock": "🪨 سنگ", "paper": "📄 کاغذ", "scissors": "✂️ قیچی"}
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}  # کلید، کدوم‌یکی رو می‌بره


def determine_winner(c1: str, c2: str) -> str:
    """برمی‌گردونه: 'P1'، 'P2' یا 'DRAW'."""
    if c1 == c2:
        return "DRAW"
    return "P1" if BEATS[c1] == c2 else "P2"


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
def create_game(chat_id: int, player1_id: int, player1_name: str, player2_id: int, player2_name: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO rps_games (chat_id, player1_id, player1_name, player2_id, player2_name, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (chat_id, player1_id, player1_name, player2_id, player2_name, datetime.utcnow().isoformat()),
    )
    conn.commit()
    game_id = c.lastrowid
    conn.close()
    return game_id


def get_game(game_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM rps_games WHERE game_id=?", (game_id,))
    row = c.fetchone()
    conn.close()
    return row


def set_choice(game_id: int, slot: int, choice: str):
    conn = get_conn()
    c = conn.cursor()
    col = "choice1" if slot == 1 else "choice2"
    c.execute(f"UPDATE rps_games SET {col}=? WHERE game_id=?", (choice, game_id))
    conn.commit()
    conn.close()


def finish_game(game_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE rps_games SET status='finished' WHERE game_id=?", (game_id,))
    conn.commit()
    conn.close()


def set_message_id(game_id: int, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE rps_games SET message_id=? WHERE game_id=?", (message_id, game_id))
    conn.commit()
    conn.close()


def record_result(user_id: int, name: str, outcome: str):
    """outcome: 'win' | 'loss' | 'draw'"""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO rps_player_stats (user_id, name, total_games, wins, losses, draws) VALUES (?, ?, 1, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, total_games=total_games+1, "
        "wins=wins+excluded.wins, losses=losses+excluded.losses, draws=draws+excluded.draws",
        (user_id, name, 1 if outcome == "win" else 0, 1 if outcome == "loss" else 0, 1 if outcome == "draw" else 0),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------
def mode_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 بازی با کاربر", callback_data="rps_mode_user")],
            [InlineKeyboardButton("🤖 بازی با ربات", callback_data="rps_mode_bot")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="games_back_main")],
        ]
    )


def _choice_keyboard(prefix: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(CHOICE_LABEL["rock"], callback_data=f"{prefix}_rock"),
                InlineKeyboardButton(CHOICE_LABEL["paper"], callback_data=f"{prefix}_paper"),
                InlineKeyboardButton(CHOICE_LABEL["scissors"], callback_data=f"{prefix}_scissors"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------
async def rps_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("_")[-1]  # user | bot
    if mode == "bot":
        await query.edit_message_text(
            "🪨📄✂️ یکی رو انتخاب کن، همین الان نتیجه رو می‌گم:",
            reply_markup=_choice_keyboard("rps_botchoice"),
        )
    else:
        game_router.set_pending(update.effective_user.id, "rps")
        await query.edit_message_text(
            "با اون کسی که می‌خوای باهاش بازی کنی، روی یکی از پیام‌هاش تو گروه ریپلای بزن و بنویس «بازی»."
        )


async def rps_botchoice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_choice = query.data.split("_")[-1]
    bot_choice = random.choice(CHOICES)
    result = determine_winner(user_choice, bot_choice)
    user = update.effective_user

    lines = [
        "🪨📄✂️ سنگ‌کاغذقیچی (با ربات)",
        f"تو: {CHOICE_LABEL[user_choice]}   —   ربات: {CHOICE_LABEL[bot_choice]}",
        "",
    ]
    if result == "DRAW":
        lines.append("🤝 مساوی شد!")
        record_result(user.id, user.first_name or "بازیکن", "draw")
    elif result == "P1":
        lines.append("🎉 بردی!")
        record_result(user.id, user.first_name or "بازیکن", "win")
    else:
        lines.append("😅 ربات برد!")
        record_result(user.id, user.first_name or "بازیکن", "loss")

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 دوباره با ربات", callback_data="rps_mode_bot")]])
    await query.edit_message_text("\n".join(lines), reply_markup=keyboard)


async def rps_challenge_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی کسی روی پیامِ یکی دیگه ریپلای بزنه و بنویسه «بازی»، درحالی‌که سنگ‌کاغذقیچی رو
    از منو انتخاب کرده بود (این تابع از bot.py، از روی تشخیصِ game_router صدا زده می‌شه)."""
    msg = update.message
    challenger = update.effective_user
    opponent = msg.reply_to_message.from_user if msg.reply_to_message else None
    if opponent is None:
        await msg.reply_text("باید روی پیامِ همون کسی که می‌خوای باهاش بازی کنی ریپلای بزنی.")
        return
    if opponent.is_bot:
        await msg.reply_text("نمی‌تونم بین تو و یه ربات دیگه بازی راه بندازم؛ یه آدم واقعی رو ریپلای بزن.")
        return
    if opponent.id == challenger.id:
        await msg.reply_text("نمی‌تونی با خودت بازی کنی! روی پیام یکی دیگه ریپلای بزن.")
        return

    game_id = create_game(
        chat_id=update.effective_chat.id,
        player1_id=challenger.id,
        player1_name=challenger.first_name or "بازیکن ۱",
        player2_id=opponent.id,
        player2_name=opponent.first_name or "بازیکن ۲",
    )
    sent = await msg.reply_text(
        f"🪨📄✂️ سنگ‌کاغذقیچی: {challenger.first_name} در مقابل {opponent.first_name}\n\n"
        "هرکدوم جدا و مخفیانه یکی از گزینه‌ها رو بزنید؛ تا وقتی هر دو انتخاب نکنید، "
        "جواب کسی لو نمی‌ره. وقتی هردو انتخاب کردید، نتیجه همینجا اعلام می‌شه 👇",
        reply_markup=_choice_keyboard(f"rps_pick_{game_id}"),
    )
    set_message_id(game_id, sent.message_id)


async def rps_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")  # ["rps", "pick", "<game_id>", "<choice>"]
    game_id = int(parts[2])
    choice = parts[3]
    game = get_game(game_id)
    if game is None or game["status"] != "active":
        await query.answer("این بازی دیگه فعال نیست.", show_alert=True)
        return

    user = update.effective_user
    if user.id == game["player1_id"]:
        slot = 1
    elif user.id == game["player2_id"]:
        slot = 2
    else:
        await query.answer("این بازی مال تو نیست!", show_alert=True)
        return

    set_choice(game_id, slot, choice)
    await query.answer("جواب ثبت شد ✅ منتظر حریفت باش...", show_alert=True)

    game = get_game(game_id)  # تازه بخونیمش تا هر دو انتخاب رو ببینیم
    if not game["choice1"] or not game["choice2"]:
        return  # هنوز نفرِ دوم انتخاب نکرده - پیامِ گروه دست‌نخورده می‌مونه، جواب لو نمی‌ره

    # هر دو انتخاب کردن - وقتشه نتیجه رو اعلام کنیم
    c1, c2 = game["choice1"], game["choice2"]
    result = determine_winner(c1, c2)
    p1_name, p2_name = game["player1_name"], game["player2_name"]

    lines = [
        "🪨📄✂️ نتیجه‌ی بازی:",
        f"{p1_name}: {CHOICE_LABEL[c1]}",
        f"{p2_name}: {CHOICE_LABEL[c2]}",
        "",
    ]
    if result == "DRAW":
        lines.append("🤝 مساوی شد!")
        record_result(game["player1_id"], p1_name, "draw")
        record_result(game["player2_id"], p2_name, "draw")
    elif result == "P1":
        lines.append(f"🎉 برد با {p1_name}!")
        record_result(game["player1_id"], p1_name, "win")
        record_result(game["player2_id"], p2_name, "loss")
    else:
        lines.append(f"🎉 برد با {p2_name}!")
        record_result(game["player1_id"], p1_name, "loss")
        record_result(game["player2_id"], p2_name, "win")

    finish_game(game_id)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 دوباره بازی", callback_data=f"rps_rematch_{game_id}")]])
    await query.edit_message_text("\n".join(lines), reply_markup=keyboard)


async def rps_rematch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    old_id = int(query.data.split("_")[-1])
    old = get_game(old_id)
    if old is None:
        return
    # نفرِ اولِ دفعه‌ی قبل، این‌بار دومی می‌شه - منصفانه‌تره
    new_id = create_game(
        chat_id=old["chat_id"],
        player1_id=old["player2_id"],
        player1_name=old["player2_name"],
        player2_id=old["player1_id"],
        player2_name=old["player1_name"],
    )
    await query.edit_message_text(
        f"🪨📄✂️ سنگ‌کاغذقیچی: {old['player2_name']} در مقابل {old['player1_name']}\n\n"
        "هرکدوم جدا و مخفیانه یکی از گزینه‌ها رو بزنید؛ تا وقتی هر دو انتخاب نکنید، جواب کسی لو نمی‌ره 👇",
        reply_markup=_choice_keyboard(f"rps_pick_{new_id}"),
    )
    set_message_id(new_id, query.message.message_id)


# ---------------------------------------------------------------------------
# ثبت هندلرها - این تابع رو از bot.py صدا بزن: rps_game.register(app)
# ---------------------------------------------------------------------------
def register(app: Application):
    app.add_handler(CallbackQueryHandler(rps_mode_callback, pattern="^rps_mode_(user|bot)$"))
    app.add_handler(CallbackQueryHandler(rps_botchoice_callback, pattern="^rps_botchoice_"))
    app.add_handler(CallbackQueryHandler(rps_pick_callback, pattern="^rps_pick_"))
    app.add_handler(CallbackQueryHandler(rps_rematch_callback, pattern="^rps_rematch_"))
