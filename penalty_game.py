# -*- coding: utf-8 -*-
"""
پنالتی - ماژول مستقل بازی برای ربات چتر (کولیبا)
=====================================================
دقیقاً هم‌سبک dooz_game.py / rps_game.py؛ فقط کافیه تو bot.py این‌طور صداش بزنی:

    import penalty_game
    ...
    penalty_game.register(app)   # داخل build_application(), قبل از برگردوندن app

قانون بازی:
- «بازی» -> از منوی مشترک، «⚽ پنالتی» رو انتخاب می‌کنی.
- دو گزینه: «👥 بازی با دوستان» و «👤 پروفایل».
- بازی با دوستان: تعداد راند رو انتخاب می‌کنی (حداکثر ۵ تا اسپم نشه)، بعد روی پیامِ
  حریف ریپلای می‌زنی و می‌نویسی «بازی» (دقیقاً مثل دوز/سنگ‌کاغذقیچی).
- نفر اول همه‌ی راندهاش رو می‌زنه، بعد نفر دوم. هر شوت: اول توپِ متحرکِ خودِ تلگرام
  (send_dice با ایموجی ⚽) پرتاب می‌شه، ربات صبر می‌کنه انیمیشن تموم بشه، بعد نتیجه
  رو اعلام می‌کنه (طبق مستندات نیمه‌رسمی تلگرام: مقدار ۴ یا ۵ = گل، ۱ تا ۳ = گلر گرفت/اوت).
- اگه بعد از پایانِ راندهای عادی مساوی بودن، وارد «ضربات سرنوشت‌ساز» می‌شیم: هر دو یه شوتِ
  اضافه می‌زنن؛ اگه یکی گل زد و اون یکی نه، بازی همون‌جا تموم می‌شه؛ وگرنه دوباره تکرار می‌شه.

سکه: هر بازیکن پیش‌فرض ۱۰ سکه داره. برای شروعِ بازی، هر دو طرف باید حداقل ۱ سکه داشته باشن
(چون این بازی رو باهاش شرط می‌بندن). آخرِ بازی، ۱ سکه از بازنده کم و به برنده اضافه می‌شه
(یعنی همون ۱ سکه‌ی شرط، از بازنده به برنده منتقل می‌شه).
"""

import os
import json
import random
import asyncio
import sqlite3
from datetime import datetime

import game_router
import pg_compat

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# دیتابیس (همون فایل chatr_bot.db که کنار bot.py هست، چندتا جدول جدا برای این بازی)
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("CHATR_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatr_bot.db"))

MAX_ROUNDS = 5
DEFAULT_COINS = 10
DICE_ANIMATION_WAIT_SECONDS = 4  # صبر تا انیمیشنِ توپِ ⚽ خودِ تلگرام کامل تموم بشه، بعد نتیجه اعلام بشه
GOAL_VALUES = (4, 5)  # طبق مستندات نیمه‌رسمی تلگرام: مقدار ۴ یا ۵ روی دایسِ ⚽ یعنی گل

# ---------------------------------------------------------------------------
# لقب‌های خریدنی (فروشگاهِ لقب): قیمت‌ها عمداً بالاتر از سکه‌ی شروعِ ۱۰ تایی چیده شدن، تا
# صرفاً «تازه اومدن» کافی نباشه و واقعاً باید یا چندبار برنده بشی یا چندروز فعال باشی
# (سکه‌ی روزانه) تا بتونی بخری. لقب‌های رونالدو/مسی طبق خواسته‌ی خودت ۳۰ سکه‌ان.
TITLE_CATALOG = [
    {"id": "ronaldo", "label": "رونالدو", "emoji": "🐐", "price": 30},
    {"id": "messi", "label": "مسی", "emoji": "🐐", "price": 30},
    {"id": "mbappe", "label": "امباپه", "emoji": "⚡", "price": 26},
    {"id": "neymar", "label": "نیمار", "emoji": "⚡", "price": 26},
    {"id": "maradona", "label": "مارادونا", "emoji": "🔱", "price": 22},
    {"id": "pele", "label": "پله", "emoji": "🔱", "price": 22},
    {"id": "zidane", "label": "زیدان", "emoji": "🎯", "price": 18},
    {"id": "beckham", "label": "بکام", "emoji": "🎯", "price": 18},
    {"id": "ronaldinho", "label": "رونالدینیو", "emoji": "✨", "price": 14},
    {"id": "modric", "label": "مودریچ", "emoji": "✨", "price": 14},
]
TITLE_BY_ID = {t["id"]: t for t in TITLE_CATALOG}

# ---------------------------------------------------------------------------
# بانک/وام: برای اونایی که سکه‌شون تموم شده و نمی‌تونن بازی کنن.
# ---------------------------------------------------------------------------
LOAN_AMOUNT = 10               # مبلغ وام
LOAN_ELIGIBLE_MAX_COINS = 5    # فقط کسایی که سکه‌شون کمتر از این باشه می‌تونن وام/طرح روزانه بگیرن
LOAN_REPAY_ASK_THRESHOLD = 20  # از این سکه به بالا، ازش می‌پرسیم می‌خوای وامت رو پس بدی؟
LOAN_REPAY_FORCE_THRESHOLD = 25  # به این سکه که برسه، اگه هنوز پس نداده باشه خودکار کم می‌شه
DAILY_PLAN_AMOUNTS = (2, 3)    # طرح روزانه: هر روز ۲ یا ۳ سکه (یه‌بار رندوم انتخاب می‌شه و ثابت می‌مونه)


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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS penalty_games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            p1_id INTEGER NOT NULL,
            p1_name TEXT,
            p2_id INTEGER NOT NULL,
            p2_name TEXT,
            rounds INTEGER NOT NULL DEFAULT 5,
            current_player INTEGER NOT NULL DEFAULT 1,
            current_round INTEGER NOT NULL DEFAULT 1,
            p1_goals INTEGER NOT NULL DEFAULT 0,
            p1_shots INTEGER NOT NULL DEFAULT 0,
            p2_goals INTEGER NOT NULL DEFAULT 0,
            p2_shots INTEGER NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'normal',
            sd_p1_goal INTEGER,
            sd_p2_goal INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS penalty_player_stats (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            total_games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            total_goals INTEGER NOT NULL DEFAULT 0,
            total_shots INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS penalty_coins (
            user_id INTEGER PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 10
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS penalty_owned_titles (
            user_id INTEGER NOT NULL,
            title_id TEXT NOT NULL,
            purchased_at TEXT,
            PRIMARY KEY (user_id, title_id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS penalty_equipped_title (
            user_id INTEGER PRIMARY KEY,
            title_id TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS penalty_bank (
            user_id INTEGER PRIMARY KEY,
            owed INTEGER NOT NULL DEFAULT 0,
            has_used_loan INTEGER NOT NULL DEFAULT 0,
            daily_plan INTEGER NOT NULL DEFAULT 0,
            daily_plan_amount INTEGER NOT NULL DEFAULT 0,
            asked_for_repay INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


_ensure_schema()


# ---------------------------------------------------------------------------
# سکه‌ها
# ---------------------------------------------------------------------------
def get_coins(user_id: int) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT coins FROM penalty_coins WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO penalty_coins (user_id, coins) VALUES (?, ?)", (user_id, DEFAULT_COINS))
        conn.commit()
        coins = DEFAULT_COINS
    else:
        coins = row["coins"]
    conn.close()
    return coins


def add_coins(user_id: int, delta: int) -> int:
    """delta می‌تونه منفی هم باشه؛ سکه هیچ‌وقت زیرِ صفر نمی‌ره."""
    current = get_coins(user_id)
    new_val = max(0, current + delta)
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE penalty_coins SET coins=? WHERE user_id=?", (new_val, user_id))
    conn.commit()
    conn.close()
    return new_val


# ---------------------------------------------------------------------------
# فروشگاه لقب‌ها
# ---------------------------------------------------------------------------
def owned_title_ids(user_id: int) -> set:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT title_id FROM penalty_owned_titles WHERE user_id=?", (user_id,))
    ids = {r["title_id"] for r in c.fetchall()}
    conn.close()
    return ids


def get_equipped_title_id(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT title_id FROM penalty_equipped_title WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row["title_id"] if row else None


def set_equipped_title_id(user_id: int, title_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO penalty_equipped_title (user_id, title_id) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET title_id=excluded.title_id",
        (user_id, title_id),
    )
    conn.commit()
    conn.close()


def buy_title(user_id: int, title_id: str):
    """برمی‌گردونه: ('ok', new_coin_balance) یا ('already_owned', None) یا ('not_enough_coins', current_coins)
    یا ('debt_blocked', None) اگه کاربر وام/طرح روزانه‌ی فعال داشته باشه."""
    title = TITLE_BY_ID.get(title_id)
    if title is None:
        return "not_found", None
    if title_id in owned_title_ids(user_id):
        return "already_owned", None
    if has_active_debt(user_id):
        return "debt_blocked", None
    coins = get_coins(user_id)
    if coins < title["price"]:
        return "not_enough_coins", coins
    new_balance = add_coins(user_id, -title["price"])
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO penalty_owned_titles (user_id, title_id, purchased_at) VALUES (?, ?, ?)",
        (user_id, title_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return "ok", new_balance


def get_display_name(user_id: int, fallback_name: str) -> str:
    """اگه کاربر یه لقب رو تجهیز کرده باشه، به‌جای اسمِ خودش با اون لقب تو بازی شناخته می‌شه."""
    title_id = get_equipped_title_id(user_id)
    if title_id and title_id in TITLE_BY_ID:
        title = TITLE_BY_ID[title_id]
        return f"{title['emoji']} {title['label']}"
    return fallback_name


# ---------------------------------------------------------------------------
# بانکِ کولیبا (وام + طرح روزانه)
# ---------------------------------------------------------------------------
def get_bank_row(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM penalty_bank WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO penalty_bank (user_id) VALUES (?)", (user_id,))
        conn.commit()
        c.execute("SELECT * FROM penalty_bank WHERE user_id=?", (user_id,))
        row = c.fetchone()
    conn.close()
    return row


def _update_bank(user_id: int, **fields):
    if not fields:
        return
    get_bank_row(user_id)  # مطمئن شو ردیفش وجود داره
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id]
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE penalty_bank SET {set_clause} WHERE user_id=?", values)
    conn.commit()
    conn.close()


def has_active_debt(user_id: int) -> bool:
    """تا وقتی وام یا طرحِ روزانه فعاله، این سکه‌ها «فقط برای بازی»ان و نمی‌شه باهاشون لقب خرید."""
    row = get_bank_row(user_id)
    return bool(row["owed"]) or bool(row["daily_plan"])


def take_loan(user_id: int) -> str:
    row = get_bank_row(user_id)
    coins = get_coins(user_id)
    if row["owed"] > 0:
        return "already_has_loan"
    if coins >= LOAN_ELIGIBLE_MAX_COINS:
        return "not_eligible"
    add_coins(user_id, LOAN_AMOUNT)
    _update_bank(user_id, owed=LOAN_AMOUNT, has_used_loan=1, asked_for_repay=0)
    return "ok"


def activate_daily_plan(user_id: int):
    row = get_bank_row(user_id)
    if not row["has_used_loan"]:
        return "loan_first", None
    if row["daily_plan"]:
        return "already_active", None
    coins = get_coins(user_id)
    if coins >= LOAN_ELIGIBLE_MAX_COINS:
        return "not_eligible", None
    amount = random.choice(DAILY_PLAN_AMOUNTS)
    _update_bank(user_id, daily_plan=1, daily_plan_amount=amount)
    return "ok", amount


def deactivate_daily_plan(user_id: int):
    _update_bank(user_id, daily_plan=0, daily_plan_amount=0)


def run_daily_plan_payouts() -> list:
    """این تابع رو bot-23.py هر روز نصفه‌شب صدا می‌زنه: به همه‌ی کاربرایی که طرحِ روزانه‌شون
    فعاله، سکه‌ی روزانه‌شون رو واریز می‌کنه. لیستی از (user_id, amount, new_balance) برمی‌گردونه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, daily_plan_amount FROM penalty_bank WHERE daily_plan=1")
    rows = c.fetchall()
    conn.close()
    results = []
    for row in rows:
        new_balance = add_coins(row["user_id"], row["daily_plan_amount"])
        results.append((row["user_id"], row["daily_plan_amount"], new_balance))
    return results


def repay_loan_now(user_id: int) -> int:
    """پرداختِ کاملِ وامِ فعلی؛ مبلغِ پس‌داده‌شده رو برمی‌گردونه."""
    row = get_bank_row(user_id)
    owed = row["owed"]
    if owed <= 0:
        return 0
    add_coins(user_id, -owed)
    _update_bank(user_id, owed=0, asked_for_repay=0)
    return owed


def bank_daily_fund_display() -> int:
    """صرفاً یه عددِ نمایشیِ باحال برای صفحه‌ی بانک (نه یه محدودیتِ واقعی)؛ هر روز عوض می‌شه ولی تو
    همون روز ثابت می‌مونه."""
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    rnd = random.Random(f"koliba-bank-{today_str}")
    return rnd.randint(200, 400)


async def check_loan_repay_prompt(bot, chat_id: int, user_id: int, display_name_text: str):
    """بعد از هر افزایشِ سکه (مثلاً بردن یه بازی) صدا زده می‌شه: اگه از حدِ آستانه گذشته باشه،
    یا می‌پرسه می‌خوای وامت رو پس بدی، یا (اگه از حد اجباری گذشته) خودکار کم می‌کنه."""
    row = get_bank_row(user_id)
    if row["owed"] <= 0:
        return
    coins = get_coins(user_id)
    if coins >= LOAN_REPAY_FORCE_THRESHOLD:
        owed = repay_loan_now(user_id)
        if owed > 0:
            try:
                await bot.send_message(
                    chat_id,
                    f"🏦 {display_name_text}، سکه‌هات به {LOAN_REPAY_FORCE_THRESHOLD} تا رسید؛ طبق قرارداد، "
                    f"{owed} سکه‌ی وامت خودکار برداشت شد.",
                )
            except Exception:
                pass
    elif coins >= LOAN_REPAY_ASK_THRESHOLD and not row["asked_for_repay"]:
        _update_bank(user_id, asked_for_repay=1)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ آره، پرداخت کن", callback_data=f"pen_loan_pay_{user_id}"),
                    InlineKeyboardButton("❌ نه فعلا", callback_data=f"pen_loan_no_{user_id}"),
                ]
            ]
        )
        try:
            await bot.send_message(
                chat_id,
                f"🏦 {display_name_text}، سکه‌هات به {coins} تا رسید. می‌خوای همین الان "
                f"{row['owed']} سکه‌ی وامت رو پس بدی؟",
                reply_markup=keyboard,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# لایه دیتابیس بازی
# ---------------------------------------------------------------------------
def create_game(chat_id, p1_id, p1_name, p2_id, p2_name, rounds) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO penalty_games "
        "(chat_id, p1_id, p1_name, p2_id, p2_name, rounds, current_player, current_round, "
        " p1_goals, p1_shots, p2_goals, p2_shots, phase, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, 0, 0, 0, 'normal', 'active', ?)",
        (chat_id, p1_id, p1_name, p2_id, p2_name, rounds, datetime.utcnow().isoformat()),
    )
    conn.commit()
    game_id = c.lastrowid
    conn.close()
    return game_id


def get_game(game_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM penalty_games WHERE game_id=?", (game_id,))
    row = c.fetchone()
    conn.close()
    return row


def _save_fields(game_id: int, fields: dict):
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [game_id]
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE penalty_games SET {set_clause} WHERE game_id=?", values)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# آمار بازیکن‌ها (پروفایل)
# ---------------------------------------------------------------------------
def _record_result(user_id: int, name: str, result: str, goals: int, shots: int):
    """result: 'win' | 'loss'"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM penalty_player_stats WHERE user_id=?", (user_id,))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO penalty_player_stats (user_id, name, total_games, wins, losses, total_goals, total_shots) "
            "VALUES (?, ?, 0, 0, 0, 0, 0)",
            (user_id, name),
        )
    col = "wins" if result == "win" else "losses"
    c.execute(
        f"UPDATE penalty_player_stats SET name=?, total_games=total_games+1, {col}={col}+1, "
        f"total_goals=total_goals+?, total_shots=total_shots+? WHERE user_id=?",
        (name, goals, shots, user_id),
    )
    conn.commit()
    conn.close()


def get_player_stats(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM penalty_player_stats WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# کیبوردها و متن‌ها
# ---------------------------------------------------------------------------
def welcome_text() -> str:
    return (
        "هی سلام! 👋 به بازی پنالتی خوش اومدی ⚽\n\n"
        "تو این بازی می‌تونی با دوستات شرط ببندی و هر راند یه پنالتی بزنید، "
        "ببینیم کی گل‌زن‌تره!"
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 بازی با دوستان", callback_data="pen_with_friends")],
            [InlineKeyboardButton("🏷 لقب‌ها", callback_data="pen_titles")],
            [InlineKeyboardButton("👤 پروفایل", callback_data="pen_profile")],
            [InlineKeyboardButton("🏦 وام", callback_data="pen_bank")],
        ]
    )


def rounds_keyboard() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(str(n), callback_data=f"pen_rounds_{n}") for n in range(1, MAX_ROUNDS + 1)]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("⬅️ بازگشت", callback_data="pen_back_main")]])


def shoot_keyboard(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🦵 شوت کن ⚽", callback_data=f"pen_shoot_{game_id}")]])


# ---------------------------------------------------------------------------
# هندلرهای منو (ورود / انتخاب حالت / پروفایل)
# ---------------------------------------------------------------------------
async def penalty_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(welcome_text(), reply_markup=main_menu_keyboard())


async def pen_back_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(welcome_text(), reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# منوی بانک (وام + طرح روزانه)
# ---------------------------------------------------------------------------
async def pen_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    coins = get_coins(user_id)
    row = get_bank_row(user_id)
    fund = bank_daily_fund_display()

    lines = [
        "🏦 بانکِ کولیبا",
        "",
        f"موجودیِ کلِ امروزِ بانک: {fund} سکه",
        f"به هر کسی که سکه‌ش از {LOAN_ELIGIBLE_MAX_COINS} تا کمتر باشه، {LOAN_AMOUNT} سکه وام می‌دیم تا بتونه بازی کنه.",
        "",
        f"🪙 سکه‌ی فعلیت: {coins}",
    ]
    buttons = []

    if row["owed"] > 0:
        lines.append(f"\n📄 الان {row['owed']} سکه به بانک بدهکاری.")
        buttons.append([InlineKeyboardButton(f"✅ همین الان {row['owed']} سکه رو پس بده", callback_data=f"pen_loan_pay_{user_id}")])
    elif coins < LOAN_ELIGIBLE_MAX_COINS:
        buttons.append([InlineKeyboardButton(f"💰 وام {LOAN_AMOUNT} سکه‌ای بگیر", callback_data="pen_bank_take_loan")])
    else:
        lines.append(f"\n(الان سکه‌ت کافیه؛ وام فقط برای کسایی که کمتر از {LOAN_ELIGIBLE_MAX_COINS} سکه دارن.)")

    if row["daily_plan"]:
        lines.append(f"\n📅 طرحِ روزانه‌ت فعاله: هر روز {row['daily_plan_amount']} سکه می‌گیری (فقط برای بازی، نه خرید لقب).")
        buttons.append([InlineKeyboardButton("❌ غیرفعال‌کردنِ طرح روزانه", callback_data="pen_bank_stop_daily")])
    elif row["has_used_loan"] and coins < LOAN_ELIGIBLE_MAX_COINS and row["owed"] == 0:
        buttons.append([InlineKeyboardButton("📅 فعال‌سازی طرح روزانه", callback_data="pen_bank_daily_plan")])

    lines.append(
        f"\n⚠️ اگه جمعِ سکه‌هات از {LOAN_REPAY_ASK_THRESHOLD} تا بگذره و وامت رو پس ندی، وقتی به "
        f"{LOAN_REPAY_FORCE_THRESHOLD} سکه برسی خودمون خودکار برش می‌داریم.\n"
        "سکه‌های وام و طرحِ روزانه فقط برای بازیِ پنالتی‌ان؛ باهاشون نمی‌شه لقب خرید."
    )
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="pen_back_main")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def pen_bank_take_loan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    result = take_loan(user_id)
    if result == "ok":
        await query.answer(f"🎉 {LOAN_AMOUNT} سکه وام گرفتی! فقط برای بازیه، نه خرید لقب.")
    elif result == "already_has_loan":
        await query.answer("همین الان یه وامِ پرداخت‌نشده داری.", show_alert=True)
    else:
        await query.answer(f"وام فقط برای کسایی هست که کمتر از {LOAN_ELIGIBLE_MAX_COINS} سکه دارن.", show_alert=True)
    await pen_bank_callback(update, context)


async def pen_bank_daily_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    result, amount = activate_daily_plan(user_id)
    if result == "ok":
        await query.answer(f"📅 طرح روزانه فعال شد! هر روز {amount} سکه می‌گیری (فقط برای بازی).")
    elif result == "loan_first":
        await query.answer("اول باید وام بگیری؛ اگه اونم تموم شد، اون‌وقت طرح روزانه باز می‌شه.", show_alert=True)
    elif result == "already_active":
        await query.answer("طرح روزانه از قبل فعاله!", show_alert=True)
    else:
        await query.answer(f"طرح روزانه فقط برای کسایی هست که کمتر از {LOAN_ELIGIBLE_MAX_COINS} سکه دارن.", show_alert=True)
    await pen_bank_callback(update, context)


async def pen_bank_stop_daily_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    deactivate_daily_plan(user_id)
    await query.answer("طرح روزانه خاموش شد.")
    await pen_bank_callback(update, context)


async def pen_loan_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    target_id = int(query.data.split("_")[-1])
    if update.effective_user.id != target_id:
        await query.answer("این دکمه مالِ تو نیست.", show_alert=True)
        return
    owed = repay_loan_now(target_id)
    if owed <= 0:
        await query.answer("وامی نداری که پس بدی!", show_alert=True)
    else:
        await query.answer(f"👍 {owed} سکه پرداخت شد!")
    try:
        await query.edit_message_text(f"✅ وامِ {owed} سکه‌ای پرداخت شد. ممنون که رفیق‌بازی درنیاوردی 😄" if owed else "وامی نداشتی.")
    except Exception:
        pass


async def pen_loan_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    target_id = int(query.data.split("_")[-1])
    if update.effective_user.id != target_id:
        await query.answer("این دکمه مالِ تو نیست.", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_text(f"باشه، فعلاً نه. ولی یادت باشه سرِ {LOAN_REPAY_FORCE_THRESHOLD} سکه خودکار کم می‌شه 😉")
    except Exception:
        pass


async def pen_with_friends_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"چند راند می‌خوای پنالتی بزنی؟ (حداکثر {MAX_ROUNDS} راند، تا اسپم نشه 😄)",
        reply_markup=rounds_keyboard(),
    )


async def pen_rounds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rounds = int(query.data.split("_")[-1])
    rounds = max(1, min(MAX_ROUNDS, rounds))
    game_router.set_pending(update.effective_user.id, "penalty", rounds)
    await query.edit_message_text(
        f"{rounds} راند رو انتخاب کردی. ⚽\n"
        "حالا با اون کسی که می‌خوای باهاش بازی کنی، روی یکی از پیام‌هاش تو گروه ریپلای بزن و بنویس «بازی»."
    )


async def pen_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    stats = get_player_stats(user.id)
    coins = get_coins(user.id)
    equipped_id = get_equipped_title_id(user.id)
    equipped_label = f"{TITLE_BY_ID[equipped_id]['emoji']} {TITLE_BY_ID[equipped_id]['label']}" if equipped_id in TITLE_BY_ID else "هیچی (اسمِ خودت)"
    lines = [f"👤 پروفایل پنالتیِ {user.first_name or 'بازیکن'}", "", f"🪙 سکه: {coins}", f"🏷 لقبِ فعال: {equipped_label}"]
    if stats is None:
        lines.append("")
        lines.append("هنوز هیچ بازی پنالتی‌ای نکردی! برو یه راند بزن ببین گل‌زنی یا نه 😉")
    else:
        lines.append("")
        lines.append(f"جمع کل بازی‌ها: {stats['total_games']}")
        lines.append(f"بردها: {stats['wins']}")
        lines.append(f"باخت‌ها: {stats['losses']}")
        lines.append(f"از {stats['total_shots']} پنالتی که تا الان زدی، {stats['total_goals']} تاش گل شد.")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="pen_back_main")]])
    await query.edit_message_text("\n".join(lines), reply_markup=keyboard)


# ---------------------------------------------------------------------------
# فروشگاه لقب‌ها
# ---------------------------------------------------------------------------
async def pen_titles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    coins = get_coins(user_id)
    owned = owned_title_ids(user_id)
    equipped_id = get_equipped_title_id(user_id)

    lines = [
        "هی سلام! 👋",
        "می‌خوای با سکه‌های بازی، لقبِ بازیکن‌های معروف رو بخری و تو بازیِ پنالتی با همون لقب شناخته بشی؟",
        "",
        f"🪙 سکه‌ی فعلیت: {coins}",
        "",
        "لقب‌های قابل‌خرید:",
    ]
    buttons = []
    for t in TITLE_CATALOG:
        if t["id"] == equipped_id:
            label = f"🌟 {t['emoji']} {t['label']} (فعاله - بزن خاموشش کن)"
            cb = f"pen_title_unequip_{t['id']}"
        elif t["id"] in owned:
            label = f"✅ {t['emoji']} {t['label']} (خریداری‌شده - بزن فعالش کن)"
            cb = f"pen_title_equip_{t['id']}"
        else:
            label = f"🔒 {t['emoji']} {t['label']} — {t['price']} سکه"
            cb = f"pen_title_buy_{t['id']}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="pen_back_main")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def pen_title_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    title_id = query.data[len("pen_title_buy_"):]
    title = TITLE_BY_ID.get(title_id)
    if title is None:
        await query.answer("این لقب دیگه وجود نداره.", show_alert=True)
        return
    result, info = buy_title(user_id, title_id)
    if result == "ok":
        await query.answer(f"🎉 لقبِ {title['label']} خریداری شد! ({info} سکه برات موند)")
    elif result == "already_owned":
        await query.answer("این لقب رو قبلاً خریدی!", show_alert=True)
    elif result == "not_enough_coins":
        await query.answer(f"سکه‌ت کافی نیست! {title['price']} سکه لازمه، تو فقط {info} تا داری.", show_alert=True)
    elif result == "debt_blocked":
        await query.answer(
            "این سکه‌ها فقط برای بازیه! تا وامت رو کامل پس ندی (یا طرح روزانه رو خاموش نکنی)، "
            "نمی‌تونی باهاشون لقب بخری. از بخش 🏦 وام می‌تونی پرداختش کنی.",
            show_alert=True,
        )
    await pen_titles_callback(update, context)


async def pen_title_equip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    title_id = query.data[len("pen_title_equip_"):]
    if title_id not in owned_title_ids(user_id):
        await query.answer("این لقب رو نداری!", show_alert=True)
        return
    set_equipped_title_id(user_id, title_id)
    await query.answer(f"🏷 لقبِ {TITLE_BY_ID[title_id]['label']} فعال شد!")
    await pen_titles_callback(update, context)


async def pen_title_unequip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    set_equipped_title_id(user_id, None)
    await query.answer("لقب خاموش شد؛ دوباره با اسمِ خودت شناخته می‌شی.")
    await pen_titles_callback(update, context)


# ---------------------------------------------------------------------------
# شروع بازی (ریپلای رو پیام حریف + نوشتنِ «بازی»)
# ---------------------------------------------------------------------------
async def penalty_challenge_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE, forced_rounds: int = 5):
    msg = update.message
    challenger = update.effective_user
    opponent = msg.reply_to_message.from_user if msg.reply_to_message else None
    if opponent is None:
        await msg.reply_text("باید روی پیامِ همون کسی که می‌خوای باهاش پنالتی بزنی ریپلای بزنی.")
        return
    if opponent.is_bot:
        await msg.reply_text("نمی‌تونم بین تو و یه ربات دیگه بازی راه بندازم؛ یه آدم واقعی رو ریپلای بزن.")
        return
    if opponent.id == challenger.id:
        await msg.reply_text("نمی‌تونی با خودت پنالتی بزنی! روی پیام یکی دیگه ریپلای بزن.")
        return

    try:
        rounds = int(forced_rounds)
    except (TypeError, ValueError):
        rounds = MAX_ROUNDS
    rounds = max(1, min(MAX_ROUNDS, rounds))

    challenger_coins = get_coins(challenger.id)
    opponent_coins = get_coins(opponent.id)
    if challenger_coins < 1:
        await msg.reply_text(
            f"سکه‌ت کافی نیست رفیق! فقط {challenger_coins} سکه داری و برای شرط‌بندیِ این بازی حداقل ۱ سکه لازمه."
        )
        return
    if opponent_coins < 1:
        await msg.reply_text(
            f"{opponent.first_name or 'حریف'} سکه‌ی کافی نداره (فقط {opponent_coins} سکه) و نمی‌تونه تو این بازی شرط ببنده."
        )
        return

    challenger_name = get_display_name(challenger.id, challenger.first_name or "بازیکن ۱")
    opponent_name = get_display_name(opponent.id, opponent.first_name or "بازیکن ۲")

    game_id = create_game(update.effective_chat.id, challenger.id, challenger_name, opponent.id, opponent_name, rounds)

    text = (
        f"⚽ پنالتی شروع شد!\n"
        f"{challenger_name} 🆚 {opponent_name}\n"
        f"تعداد راند: {rounds} (هر نفر)\n"
        f"شرط: ۱ سکه 🪙\n\n"
        f"خب اول {challenger_name} شروع می‌کنه، بزن ببینم!"
    )
    await msg.reply_text(text, reply_markup=shoot_keyboard(game_id))


# ---------------------------------------------------------------------------
# منطق شوت زدن
# ---------------------------------------------------------------------------
def _finalize_and_reward(game) -> dict:
    """بازی رو تموم‌شده علامت می‌زنه، سکه/آمار رو به‌روز می‌کنه و اطلاعاتِ لازم برای پیامِ پایانی رو برمی‌گردونه."""
    p1_goals, p2_goals = game["p1_goals"], game["p2_goals"]
    if p1_goals > p2_goals:
        winner_id, winner_name, winner_goals, winner_shots = game["p1_id"], game["p1_name"], p1_goals, game["p1_shots"]
        loser_id, loser_name, loser_goals, loser_shots = game["p2_id"], game["p2_name"], p2_goals, game["p2_shots"]
    else:
        winner_id, winner_name, winner_goals, winner_shots = game["p2_id"], game["p2_name"], p2_goals, game["p2_shots"]
        loser_id, loser_name, loser_goals, loser_shots = game["p1_id"], game["p1_name"], p1_goals, game["p1_shots"]

    _record_result(winner_id, winner_name, "win", winner_goals, winner_shots)
    _record_result(loser_id, loser_name, "loss", loser_goals, loser_shots)
    winner_coins = add_coins(winner_id, 1)
    loser_coins = add_coins(loser_id, -1)

    _save_fields(game["game_id"], {"status": "finished", "phase": "finished"})

    winner_stats = get_player_stats(winner_id)
    loser_stats = get_player_stats(loser_id)

    return {
        "winner_name": winner_name,
        "loser_name": loser_name,
        "p1_goals": p1_goals,
        "p2_goals": p2_goals,
        "winner_coins": winner_coins,
        "loser_coins": loser_coins,
        "winner_wins": winner_stats["wins"] if winner_stats else 1,
        "winner_losses": winner_stats["losses"] if winner_stats else 0,
        "loser_wins": loser_stats["wins"] if loser_stats else 0,
        "loser_losses": loser_stats["losses"] if loser_stats else 1,
    }


async def pen_shoot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game_id = int(query.data.split("_")[-1])
    game = get_game(game_id)
    if game is None or game["status"] != "active":
        await query.answer("این بازی دیگه فعال نیست.", show_alert=True)
        return

    is_sudden = game["phase"] == "sudden"
    shooter_slot = game["current_player"]
    shooter_id = game["p1_id"] if shooter_slot == 1 else game["p2_id"]
    shooter_name = game["p1_name"] if shooter_slot == 1 else game["p2_name"]

    if update.effective_user.id != shooter_id:
        await query.answer("نوبت تو نیست! صبر کن حریفت بزنه.", show_alert=True)
        return

    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    chat_id = update.effective_chat.id
    dice_msg = await context.bot.send_dice(chat_id, emoji="⚽")
    value = dice_msg.dice.value
    await asyncio.sleep(DICE_ANIMATION_WAIT_SECONDS)  # صبر تا انیمیشنِ توپ کامل تموم بشه، بعد نتیجه اعلام بشه

    goal = value in GOAL_VALUES
    result_line = f"⚽🥅 {shooter_name} گل کرد!" if goal else f"🧤 گلرِ حریف گرفتش! {shooter_name} اوت زد."

    # --- فاز عادی ---
    if not is_sudden:
        goals_field = "p1_goals" if shooter_slot == 1 else "p2_goals"
        shots_field = "p1_shots" if shooter_slot == 1 else "p2_shots"
        updates = {shots_field: game[shots_field] + 1}
        if goal:
            updates[goals_field] = game[goals_field] + 1

        finished_turn = game["current_round"] >= game["rounds"]
        if not finished_turn:
            updates["current_round"] = game["current_round"] + 1
            _save_fields(game_id, updates)
            await context.bot.send_message(
                chat_id,
                f"{result_line}\n\nراند بعدی برای {shooter_name}! 🦵",
                reply_markup=shoot_keyboard(game_id),
            )
            return

        # این نفر راندهاش تموم شد
        new_goals = updates.get(goals_field, game[goals_field])
        new_shots = updates[shots_field]
        summary = (
            f"{result_line}\n\n"
            f"🔚 نتیجه‌ی {shooter_name}: از {game['rounds']} تا پنالتی، {new_goals} تا گل شد "
            f"و {new_shots - new_goals} تا رفت بیرون."
        )

        if shooter_slot == 1:
            updates["current_player"] = 2
            updates["current_round"] = 1
            _save_fields(game_id, updates)
            await context.bot.send_message(
                chat_id,
                f"{summary}\n\nحالا نوبت {game['p2_name']}! بزن ⚽",
                reply_markup=shoot_keyboard(game_id),
            )
            return

        # نفر دوم هم تموم کرد -> مقایسه
        _save_fields(game_id, updates)
        game = get_game(game_id)
        if game["p1_goals"] != game["p2_goals"]:
            result = _finalize_and_reward(game)
            await context.bot.send_message(chat_id, summary)
            await context.bot.send_message(chat_id, _final_result_text(result))
            winner_id = game["p1_id"] if game["p1_goals"] > game["p2_goals"] else game["p2_id"]
            await check_loan_repay_prompt(context.bot, chat_id, winner_id, result["winner_name"])
            return

        # مساوی -> ضربات سرنوشت‌ساز
        _save_fields(game_id, {"phase": "sudden", "current_player": 1, "sd_p1_goal": None, "sd_p2_goal": None})
        await context.bot.send_message(
            chat_id,
            f"{summary}\n\n"
            f"🔥 مساوی شدن ({game['p1_goals']}-{game['p2_goals']})! وقتِ ضربات سرنوشت‌سازه.\n"
            f"اول {game['p1_name']} بزنه!",
            reply_markup=shoot_keyboard(game_id),
        )
        return

    # --- فاز ضربات سرنوشت‌ساز ---
    shots_field = "p1_shots" if shooter_slot == 1 else "p2_shots"
    goals_field = "p1_goals" if shooter_slot == 1 else "p2_goals"
    updates = {shots_field: game[shots_field] + 1}
    if goal:
        updates[goals_field] = game[goals_field] + 1

    if shooter_slot == 1:
        updates["sd_p1_goal"] = int(goal)
        updates["current_player"] = 2
        _save_fields(game_id, updates)
        await context.bot.send_message(
            chat_id,
            f"{result_line}\n\nحالا نوبت {game['p2_name']} برای پاسخ! ⚽",
            reply_markup=shoot_keyboard(game_id),
        )
        return

    # شوتِ نفر دوم تو سرنوشت‌ساز
    updates["sd_p2_goal"] = int(goal)
    _save_fields(game_id, updates)
    game = get_game(game_id)
    p1_scored = bool(game["sd_p1_goal"])
    p2_scored = bool(game["sd_p2_goal"])

    if p1_scored != p2_scored:
        result = _finalize_and_reward(game)
        await context.bot.send_message(chat_id, result_line)
        await context.bot.send_message(chat_id, _final_result_text(result))
        winner_id = game["p1_id"] if p1_scored else game["p2_id"]
        await check_loan_repay_prompt(context.bot, chat_id, winner_id, result["winner_name"])
        return

    # بازم مساوی موند -> یه دور دیگه
    _save_fields(game_id, {"current_player": 1, "sd_p1_goal": None, "sd_p2_goal": None})
    await context.bot.send_message(
        chat_id,
        f"{result_line}\n\n😳 بازم مساوی موند! یه ضربه‌ی دیگه‌ی سرنوشت‌ساز، اول {game['p1_name']} بزنه.",
        reply_markup=shoot_keyboard(game_id),
    )


def _final_result_text(result: dict) -> str:
    return (
        f"🏆 برنده شد: {result['winner_name']}! ({result['p1_goals']}-{result['p2_goals']})\n\n"
        f"🪙 {result['winner_name']}: +۱ سکه (الان {result['winner_coins']} سکه داره)\n"
        f"🪙 {result['loser_name']}: -۱ سکه (الان {result['loser_coins']} سکه داره)\n\n"
        f"📊 {result['winner_name']}: {result['winner_wins']} برد، {result['winner_losses']} باخت\n"
        f"📊 {result['loser_name']}: {result['loser_wins']} برد، {result['loser_losses']} باخت"
    )


# ---------------------------------------------------------------------------
# ثبت هندلرها - این تابع رو از bot.py صدا بزن: penalty_game.register(app)
# ---------------------------------------------------------------------------
def register(app: Application):
    app.add_handler(CommandHandler("penalty", penalty_command_entry), group=2)
    app.add_handler(
        MessageHandler(filters.Regex(r"^پنالتی$") & (filters.ChatType.GROUPS | filters.ChatType.PRIVATE), penalty_command_entry),
        group=2,
    )
    app.add_handler(CallbackQueryHandler(pen_with_friends_callback, pattern="^pen_with_friends$"))
    app.add_handler(CallbackQueryHandler(pen_rounds_callback, pattern="^pen_rounds_\\d+$"))
    app.add_handler(CallbackQueryHandler(pen_profile_callback, pattern="^pen_profile$"))
    app.add_handler(CallbackQueryHandler(pen_titles_callback, pattern="^pen_titles$"))
    app.add_handler(CallbackQueryHandler(pen_title_buy_callback, pattern="^pen_title_buy_[a-z]+$"))
    app.add_handler(CallbackQueryHandler(pen_title_equip_callback, pattern="^pen_title_equip_[a-z]+$"))
    app.add_handler(CallbackQueryHandler(pen_title_unequip_callback, pattern="^pen_title_unequip_[a-z]+$"))
    app.add_handler(CallbackQueryHandler(pen_bank_callback, pattern="^pen_bank$"))
    app.add_handler(CallbackQueryHandler(pen_bank_take_loan_callback, pattern="^pen_bank_take_loan$"))
    app.add_handler(CallbackQueryHandler(pen_bank_daily_plan_callback, pattern="^pen_bank_daily_plan$"))
    app.add_handler(CallbackQueryHandler(pen_bank_stop_daily_callback, pattern="^pen_bank_stop_daily$"))
    app.add_handler(CallbackQueryHandler(pen_loan_pay_callback, pattern="^pen_loan_pay_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(pen_loan_no_callback, pattern="^pen_loan_no_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(pen_back_main_callback, pattern="^pen_back_main$"))
    app.add_handler(CallbackQueryHandler(pen_shoot_callback, pattern="^pen_shoot_\\d+$"))
