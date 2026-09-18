# -*- coding: utf-8 -*-
"""
ربات چتر - ربات مدیریت گروه تلگرام با پایتون
نیازمندی‌ها: python-telegram-bot==20.7  (pip install python-telegram-bot==20.7)
اجرا: یک توکن از @BotFather بگیر و در متغیر BOT_TOKEN پایین قرار بده (یا در متغیر محیطی BOT_TOKEN ست کن)
دیتابیس: SQLite (فایل chatr_bot.db به صورت خودکار کنار همین فایل ساخته می‌شه)
"""

import os
import re
import ast
import operator
import html
import json
import random
import sqlite3
import logging
import asyncio
import urllib.request
from datetime import datetime, timezone, timedelta, time as dt_time

try:
    import jdatetime  # اختیاری: اگه نصب باشه، تاریخ‌ها شمسی نشون داده می‌شن (pip install jdatetime)
    _HAS_JDATETIME = True
except ImportError:
    _HAS_JDATETIME = False


TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))


def format_persian_date_time(dt: datetime = None):
    """اگه jdatetime نصب باشه تاریخ شمسی، وگرنه میلادی برمی‌گردونه. خروجی: (تاریخ, ساعت) - همیشه به وقتِ تهران
    (UTC+۳:۳۰)، صرف‌نظر از تایم‌زونِ سروری که ربات روش اجرا می‌شه."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # همه‌جای کد زمان‌ها با utcnow() ذخیره می‌شن، پس بدونِ تایم‌زون یعنی UTC
    dt = dt.astimezone(TEHRAN_TZ)
    if _HAS_JDATETIME:
        jdt = jdatetime.datetime.fromgregorian(datetime=dt)
        return jdt.strftime("%Y/%m/%d"), jdt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    BotCommand,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ChatMemberHandler,
    ContextTypes,
    ExtBot,
    ApplicationHandlerStop,
    TypeHandler,
    filters,
)

import dooz_game  # بازی دوز - ماژول کاملاً جدا (dooz_game.py کنار همین فایل)
import rps_game  # بازی سنگ‌کاغذقیچی - ماژول کاملاً جدا (rps_game.py کنار همین فایل)
import chess_game  # بازی شطرنج - ماژول کاملاً جدا (chess_game.py کنار همین فایل)
import penalty_game  # بازی پنالتی - ماژول کاملاً جدا (penalty_game.py کنار همین فایل)
import game_router  # هماهنگ‌کننده‌ی سبک بین بازی‌ها (برای «بازی» به‌عنوان ریپلای)
import pg_compat  # لایه‌ی سازگاریِ PostgreSQL (فقط وقتی DATABASE_URL ست باشه فعال می‌شه)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("chatr_bot")

# ---------------------------------------------------------------------------
# تنظیمات پایه
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT-YOUR-TOKEN-HERE")
DB_PATH = os.environ.get("CHATR_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatr_bot.db"))
MAX_WARNINGS = 3          # بعد از این تعداد اخطار، کاربر به صورت خودکار از گروه حذف می‌شه
MESSAGES_TO_KEEP = 2000   # حداکثر تعداد آی‌دی پیام ذخیره شده برای هر گروه (برای قابلیت حذف پیام‌ها)
MAX_SPAM_MUTE_MINUTES = 30  # حداکثر مجاز برای دقیقه‌ی سکوت خودکار ضد اسپم

# آی‌دی عددی مالک اصلی ربات (تو). هر کسی که آی‌دیش اینجا باشه، تو هر گروهی که ربات عضوشه،
# فارغ از اینکه واقعا ادمین/مالک همون گروه باشه یا نه، دسترسی کامل ادمین و مالک داره.
# می‌تونی مستقیم عدد آی‌دیت رو اینجا بنویسی، یا با متغیر محیطی SUPER_ADMIN_IDS (با کاما جدا) ست کنی.
SUPER_ADMIN_IDS = set()
_env_super_admins = os.environ.get("SUPER_ADMIN_IDS", "")
if _env_super_admins:
    SUPER_ADMIN_IDS |= {int(x.strip()) for x in _env_super_admins.split(",") if x.strip().isdigit()}
# مثال دستی: SUPER_ADMIN_IDS.add(123456789)

# سوپر مالک: یه نقشِ کاملاً جدا و بالاتر از SUPER_ADMIN_IDS - فقط برای دیدنِ فهرستِ همه‌ی
# گروه‌هایی که ربات توشونه (برای چک‌کردنِ اینکه ربات رو به گروهِ نامناسبی نبردن). سوپرادمین‌های
# قبلی همون‌جوری که بودن دست‌نخورده می‌مونن؛ این یه نقشِ کاملاً اضافه و جداگونه‌ست.
SUPER_OWNER_ID = int(os.environ.get("SUPER_OWNER_ID", "7430881772"))


def get_all_group_chat_ids():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM group_settings")
    return [r["chat_id"] for r in c.fetchall()]


def is_super_owner(user_id: int) -> bool:
    return user_id == SUPER_OWNER_ID

# پک‌های دیالوگ (لحن‌ها) که از فایل‌های راو گیت‌هاب خونده می‌شن. هر لحن یه متغیر محیطی جدا داره؛
# اگه متغیرش خالی باشه یعنی هنوز فایلش آماده نیست. چندتا لحن می‌تونن هم‌زمان برای یه گروه فعال باشن.
# پک‌های دیالوگ (لحن‌ها). برای هرکدوم لینک raw فایلش رو همین‌جا جلوی "url" بذار (بین "" خالی).
# اگه ترجیح می‌دی به‌جاش با متغیر محیطی کار کنی، همون هم پشتیبانی می‌شه (اگه env ست شده باشه اون اولویت داره).
PACK_DEFINITIONS = {
    "default": {"label": "🗨 عمومی (پیش‌فرض)", "url_env": "DEFAULT_PACK_URL", "url": ""},
    "angry": {"label": "😠 عصبانی", "url_env": "ANGRY_PACK_URL", "url": ""},
    "sad": {"label": "😢 ناراحت", "url_env": "SAD_PACK_URL", "url": ""},
    "happy": {"label": "😄 خوشحال", "url_env": "HAPPY_PACK_URL", "url": ""},
}


def pack_url(info: dict) -> str:
    """اول متغیر محیطی رو چک می‌کنه؛ اگه ست نبود، لینکی که مستقیم تو PACK_DEFINITIONS نوشتی رو برمی‌گردونه."""
    return os.environ.get(info["url_env"], "") or info.get("url", "")


DEFAULT_PACK_REFRESH_SECONDS = 3600  # هر چند وقت یک‌بار خودکار از گیت‌هاب دوباره خونده بشه
_pack_cache = {}  # pack_name -> {"data": {...}, "loaded_at": datetime | None}

# جملات پیش‌فرض «یادآوری غیبت اعضا» - هر چند وقت یک‌بار ربات یکی رو تصادفی تگ می‌کنه و یکی از این جمله‌ها رو می‌گه
ENGAGEMENT_PHRASES = [
    "هی خوشگل، کجایی؟ نیستی کم پیدایی! 🌸",
    "بدون تو گپ کویره، اگه میشه برگرد ☂️",
    "کجا گمی رفیق؟ جات اینجا خالیه!",
    "چقدر وقته پیدات نیست، یه سر بزن ببینیم چه خبر!",
    "دلمون برات تنگ شد، بیا یه گپی بزنیم!",
    "گروه بدون تو رنگ و بو نداره، بیا دیگه!",
    "پس چرا انقدر کم‌پیدایی؟ منتظرتیم!",
    "یه سر بزن حداقل، دلمون تنگ شده!",
    "کجایی که این‌جا سوت و کوره بدون تو!",
    "بیا یه چیزی بگو، جات خالیه اینجا!",
]

# اسم ربات - وقتی جایی تو پیام باشه، ربات یکی از این ۱۰ جواب رو رندوم جواب می‌ده (همیشه فعاله، مستقل از پک‌ها)
BOT_NAME = "کولیبا"
BOT_NAME_COMMON_MISSPELLING = "کولبیا"  # املای رایجِ اشتباه (جابه‌جاییِ حروف) که خیلی‌ها به‌جاش می‌نویسن
BOT_NAME_REACTIONS = [
    "جونم؟ صدام کردی؟ 😄",
    "بله بله، در خدمتم!",
    "کولیبا حاضره، بفرما!",
    "جانم عزیز، چیزی شده؟",
    "بله؟ اینجام، چیکار داری؟",
    "کولیبا شنیدم، بگو ببینم!",
    "هوی، صدام کردی! چه خبر؟",
    "بله سرورم، در خدمتم! 🫡",
    "جونم رفیق، گوش می‌دم!",
    "کولیبا اینجاست، بگو چی شده!",
]

# پیام‌های خوشامد برای کاربری که قبلاً تو گروه بوده، رفته، و برگشته
WELCOME_BACK_PHRASES = [
    "برگشتی رفیق! خوش اومدی، جات خالی بود.",
    "به‌به، برگشتی! دلمون برات تنگ شده بود.",
    "خوش اومدی، می‌دونستم برمی‌گردی!",
    "برگشتی که! گروه بدون تو یه‌چیزیش کم بود.",
    "سلام دوباره! خوش برگشتی رفیق.",
]

# مراحل مکالمه (ConversationHandler states)
(
    LEARN_WORD_WAIT_TRIGGER,
    LEARN_WORD_WAIT_ANSWER,
    EDIT_WORD_WAIT_ANSWER,
    ADD_TITLE_WAIT_USER,
    ADD_TITLE_WAIT_TEXT,
    WARN_WAIT_USER,
    WARN_WAIT_REASON,
    REMOVE_WARN_WAIT_USER,
    FORBIDDEN_ADD_WAIT_WORD,
    FORBIDDEN_REMOVE_WAIT_WORD,
    DELETE_N_WAIT_NUMBER,
    SPECIAL_ADD_WAIT_USER,
    SPECIAL_TONE_WAIT_TEXT,
    DELETE_WORD_WAIT_CODE,
    SPAM_CFG_WAIT_THRESHOLD,
    SPAM_CFG_WAIT_MUTE,
    ENGAGEMENT_WAIT_HOURS,
    WELCOME_MEDIA_WAIT,
    BROADCAST_WAIT_CONTENT,
    SUPERPANEL_XP_WAIT_USER,
    SUPERPANEL_XP_WAIT_AMOUNT,
    SUPERPANEL_CHANNEL_BAN_WAIT_USER,
) = range(22)

# ---- سکه‌ی روزانه (نصفه‌شب به وقت تهران) -----------------------------------
SPAM_WINDOW_SECONDS = 8.0    # این تعداد پیام باید داخل همین بازه‌ی زمانی بیاد تا اسپم حساب بشه (استانداردِ رایج بین رباتا ۵ تا ۱۰ ثانیه‌ست)

# ---------------------------------------------------------------------------
# عضویت اجباری تو کانال (فقط تو پیویِ ربات، قبل از نمایشِ منوی اصلی)
# REQUIRED_CHANNEL_ID باید عددیِ چتِ کانال باشه (شبیه -1001234567890)، نه لینکِ دعوت.
# تا وقتی این متغیرِ محیطی ست نشده، این محدودیت کلاً غیرفعاله و همه‌چی عادی کار می‌کنه.
# راهِ پیدا کردنِ عددیِ کانالِ خصوصی: یه پیام از کانال رو فوروارد کن به @userinfobot یا
# @RawDataBot و آیدیِ عددیِ چت (که با -100 شروع می‌شه) رو از اونجا بردار.
# ---------------------------------------------------------------------------
REQUIRED_CHANNEL_ID = os.environ.get("REQUIRED_CHANNEL_ID", "").strip()
REQUIRED_CHANNEL_URL = os.environ.get("REQUIRED_CHANNEL_URL", "https://t.me/+-YgTrM-AH6JmYjc0")

# ---------------------------------------------------------------------------
# نسخه‌ی ربات - هر آپدیت که میاد این عدد رو دستی زیاد کن
# ---------------------------------------------------------------------------
BOT_VERSION = "7.9"


# ---------------------------------------------------------------------------
# لایه دیتابیس
# ---------------------------------------------------------------------------
class _PersistentConnProxy:
    """
    یه نماینده‌ی شفاف روی یه کانکشنِ SQLite که فقط یه‌بار باز می‌شه و برای همیشه (تا وقتی
    پروسه زنده‌ست) باز می‌مونه. همه‌جای این فایل، بعد از هر کار با دیتابیس conn.close() صدا
    زده می‌شه - این‌جا close() عمداً هیچ کاری نمی‌کنه (کانکشنِ واقعی رو نمی‌بنده)، پس نیازی
    نیست حتی یک خط از اون ۱۵۰+ جایی که get_conn() صدا زده می‌شه عوض بشه؛ فقط دیگه هر پیام
    مجبور نیست از نو فایل رو باز کنه و PRAGMAها رو دوباره تنظیم کنه - که این تنها بخشِ کندِ
    واقعیِ لایه‌ی دیتابیس بود.
    """
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


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS learned_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            teacher_id INTEGER NOT NULL DEFAULT 0,
            code INTEGER NOT NULL,
            trigger_word TEXT NOT NULL,
            answers TEXT NOT NULL,      -- JSON list of strings
            UNIQUE(chat_id, teacher_id, code)
        );

        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            reasons TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS forbidden_words (
            chat_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            PRIMARY KEY (chat_id, word)
        );

        CREATE TABLE IF NOT EXISTS special_members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            custom_tone TEXT,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS banned_users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            banned_at TEXT,
            reason TEXT,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS active_challenges (
            chat_id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            answer INTEGER NOT NULL,
            question_text TEXT,
            created_at TEXT,
            last_reposted_at TEXT,
            resolved INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS active_bonus_challenges (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            answer INTEGER NOT NULL,
            question_text TEXT,
            created_at TEXT,
            resolved INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS user_levels (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS user_subject_levels (
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            correct_count INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, subject)
        );

        CREATE TABLE IF NOT EXISTS super_panel_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            bot_enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS channel_banned_users (
            user_id INTEGER PRIMARY KEY,
            banned_at TEXT
        );

        CREATE TABLE IF NOT EXISTS group_settings (
            chat_id INTEGER PRIMARY KEY,
            locked INTEGER NOT NULL DEFAULT 0,
            forward_allowed INTEGER NOT NULL DEFAULT 1,
            spam_protection INTEGER NOT NULL DEFAULT 0,
            spam_threshold INTEGER NOT NULL DEFAULT 5,
            spam_mute_minutes INTEGER NOT NULL DEFAULT 5,
            allow_photo INTEGER NOT NULL DEFAULT 1,
            allow_video INTEGER NOT NULL DEFAULT 1,
            allow_sticker_gif INTEGER NOT NULL DEFAULT 1,
            owner_user_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS tracked_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS message_stats (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            text_count INTEGER NOT NULL DEFAULT 0,
            photo_count INTEGER NOT NULL DEFAULT 0,
            video_count INTEGER NOT NULL DEFAULT 0,
            sticker_count INTEGER NOT NULL DEFAULT 0,
            gif_count INTEGER NOT NULL DEFAULT 0,
            other_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS user_groups (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            title TEXT,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS group_members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            first_name TEXT,
            username TEXT,
            updated_at TEXT,
            first_seen TEXT,
            is_present INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS active_packs (
            chat_id INTEGER NOT NULL,
            pack_name TEXT NOT NULL,
            PRIMARY KEY (chat_id, pack_name)
        );

        CREATE TABLE IF NOT EXISTS koliba_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            note_text TEXT NOT NULL,
            created_at TEXT,
            mentioned_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mutes (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            muted_at TEXT,
            until_ts INTEGER,
            reason TEXT,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS nicknames (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            set_by INTEGER,
            created_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS pv_chat_links (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            linked_at TEXT
        );
        """
    )
    conn.commit()

    # --- مهاجرت امن برای دیتابیس‌های قدیمی‌تر که این ستون‌ها/محدودیت‌ها رو نداشتن ---
    def _column_names(table):
        c.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in c.fetchall()]

    def _add_column_if_missing(table, coldef):
        col_name = coldef.split()[0]
        try:
            if col_name not in _column_names(table):
                c.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
                conn.commit()
        except Exception as e:
            # یه ستون که اضافه نشه نباید جلوی بقیه‌ی ستون‌های بعدیِ همین لیست رو بگیره -
            # وگرنه یه مشکلِ گذرا رو یه ستون، همه‌ی ستون‌های بعدی (حتی کاملاً بی‌ربط) رو
            # برای همیشه از اضافه‌شدن باز می‌داره، بدونِ اینکه هیچ خطایی هم دیده بشه.
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"⚠️ نتونستم ستونِ «{col_name}» رو به جدولِ «{table}» اضافه کنم: {e}")

    for coldef in (
        "spam_threshold INTEGER NOT NULL DEFAULT 5",
        "spam_mute_minutes INTEGER NOT NULL DEFAULT 5",
        "allow_photo INTEGER NOT NULL DEFAULT 1",
        "allow_video INTEGER NOT NULL DEFAULT 1",
        "allow_sticker_gif INTEGER NOT NULL DEFAULT 1",
        "owner_user_id INTEGER",
        "use_default_pack INTEGER NOT NULL DEFAULT 0",
        "block_links INTEGER NOT NULL DEFAULT 0",
        "engagement_enabled INTEGER NOT NULL DEFAULT 0",
        "engagement_interval_hours INTEGER NOT NULL DEFAULT 6",
        "last_engagement_at TEXT",
        "welcome_messages_enabled INTEGER NOT NULL DEFAULT 1",
        "admin_actions_on_owner INTEGER NOT NULL DEFAULT 0",
        "bot_paused INTEGER NOT NULL DEFAULT 0",
        "spam_warn_enabled INTEGER NOT NULL DEFAULT 0",
        "welcome_media_file_id TEXT",
        "welcome_media_type TEXT",
        "challenges_enabled INTEGER NOT NULL DEFAULT 1",
        "next_challenge_at INTEGER NOT NULL DEFAULT 0",
        "challenge_interval_seconds INTEGER NOT NULL DEFAULT 900",
        "next_bonus_challenge_at INTEGER NOT NULL DEFAULT 0",
        "challenge_difficulty TEXT NOT NULL DEFAULT 'normal'",
    ):
        _add_column_if_missing("group_settings", coldef)

    for coldef in (
        "question_text TEXT",
        "last_reposted_at TEXT",
    ):
        _add_column_if_missing("active_challenges", coldef)

    for coldef in (
        "points INTEGER NOT NULL DEFAULT 0",
    ):
        _add_column_if_missing("user_subject_levels", coldef)

    # مهاجرتِ یک‌بارِ امتیاز: قبل از اضافه‌شدنِ سیستمِ امتیازی، فقط «تعدادِ جوابِ درست» ذخیره
    # می‌شد. اگه این مهاجرت نباشه، کسی که قبلاً مثلاً ۱۵ تا جوابِ درست داده، بعد از این آپدیت
    # points=0 می‌مونه و سطحش یهو می‌ریزه به ۱ - این یه باگِ واقعیه که همینجا رفعش می‌کنیم.
    try:
        c.execute("UPDATE user_subject_levels SET points = correct_count * 10 WHERE points = 0 AND correct_count > 0")
        conn.commit()
    except Exception:
        pass

    for coldef in (
        "mode TEXT NOT NULL DEFAULT 'all'",
        "exempt_photo INTEGER NOT NULL DEFAULT 1",
        "exempt_video INTEGER NOT NULL DEFAULT 1",
        "exempt_sticker INTEGER NOT NULL DEFAULT 1",
        "exempt_forward INTEGER NOT NULL DEFAULT 1",
        "exempt_links INTEGER NOT NULL DEFAULT 1",
        "exempt_words INTEGER NOT NULL DEFAULT 1",
        "exempt_spam INTEGER NOT NULL DEFAULT 1",
    ):
        _add_column_if_missing("special_members", coldef)

    for coldef in (
        "first_seen TEXT",
        "is_present INTEGER NOT NULL DEFAULT 1",
    ):
        _add_column_if_missing("group_members", coldef)

    # برای رکوردهای قدیمیِ اعضا که first_seen ندارن، از همون updated_at به عنوان اولین دیدار استفاده می‌کنیم
    c.execute("UPDATE group_members SET first_seen = updated_at WHERE first_seen IS NULL")
    conn.commit()

    # مهاجرت از سیستم قدیمی تک‌پک (use_default_pack) به سیستم چندپکِ جدید (active_packs)
    c.execute("SELECT chat_id FROM group_settings WHERE use_default_pack=1")
    for row in c.fetchall():
        c.execute(
            "INSERT OR IGNORE INTO active_packs (chat_id, pack_name) VALUES (?, 'default')", (row["chat_id"],)
        )
    conn.commit()

    # اگه learned_words قدیمی (بدون teacher_id) باشه، جدولش رو با نگه‌داشتن داده‌ها بازسازی می‌کنیم
    if "teacher_id" not in _column_names("learned_words"):
        c.executescript(
            """
            ALTER TABLE learned_words RENAME TO learned_words_old;
            CREATE TABLE learned_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                teacher_id INTEGER NOT NULL DEFAULT 0,
                code INTEGER NOT NULL,
                trigger_word TEXT NOT NULL,
                answers TEXT NOT NULL,
                UNIQUE(chat_id, teacher_id, code)
            );
            INSERT INTO learned_words (id, chat_id, teacher_id, code, trigger_word, answers)
                SELECT id, chat_id, 0, code, trigger_word, answers FROM learned_words_old;
            DROP TABLE learned_words_old;
            """
        )
        conn.commit()

    conn.close()


# ---- گروه‌های شناخته‌شده هر کاربر (برای اینکه بشه تو پیوی هم کلمه یاد داد) --
def record_user_group(user_id: int, chat_id: int, title: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO user_groups (user_id, chat_id, title) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, chat_id) DO UPDATE SET title=excluded.title",
        (user_id, chat_id, title),
    )
    conn.commit()
    conn.close()


def get_user_groups(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, title FROM user_groups WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_pv_link(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM pv_chat_links WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def set_pv_link(user_id: int, chat_id: int, chat_title: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO pv_chat_links (user_id, chat_id, chat_title, linked_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id, chat_title=excluded.chat_title, linked_at=excluded.linked_at",
        (user_id, chat_id, chat_title, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def remove_pv_link(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM pv_chat_links WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def _group_settings_exists(chat_id: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM group_settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


async def find_admin_groups_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """گروه‌هایی که این کاربر توشون شناخته شده (حداقل یه پیام داده) و الان هم واقعاً
    ادمین/مالک اونجاست رو برمی‌گردونه: لیستی از (chat_id, title)."""
    candidates = get_user_groups(user_id)
    result = []
    seen = set()
    for row in candidates:
        chat_id = row["chat_id"]
        if chat_id in seen or not _group_settings_exists(chat_id):
            continue
        seen.add(chat_id)
        try:
            if await is_user_admin(update, context, user_id, chat_id):
                result.append((chat_id, row["title"] or f"گروه {chat_id}"))
        except Exception:
            continue
    return result


def row_get(row, key, default=None):
    """دسترسیِ امن به یه ستون از sqlite3.Row یا pg_compat.Row - اگه ستون به هر دلیلی
    هنوز رو دیتابیس نباشه (مثلاً مهاجرتِ یه ستونِ جدید هنوز کامل نشده)، به‌جای کرش‌کردن
    مقدارِ پیش‌فرض رو برمی‌گردونه."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def get_settings(chat_id: int) -> sqlite3.Row:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM group_settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO group_settings (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        c.execute("SELECT * FROM group_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    conn.close()
    return row


async def get_group_owner_id(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    آی‌دی مالک واقعی گروه رو برمی‌گردونه (کش‌شده تو دیتابیس). فقط لیست کلمات همین آدم
    تو گروه استفاده می‌شه، تا لیست هر ادمین دیگه‌ای خصوصیِ خودش بمونه.
    """
    s = get_settings(chat_id)
    if s["owner_user_id"]:
        return s["owner_user_id"]
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for m in admins:
            if m.status == ChatMemberStatus.OWNER:
                conn = get_conn()
                c = conn.cursor()
                c.execute("UPDATE group_settings SET owner_user_id=? WHERE chat_id=?", (m.user.id, chat_id))
                conn.commit()
                conn.close()
                return m.user.id
    except Exception:
        pass
    return None


async def is_owner_protected(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_user_id: int) -> bool:
    """
    True یعنی: این کاربر مالک واقعی گروهه و طبق تنظیمات فعلیِ گروه، قابلیت‌های ادمین
    (سکوت/اخطار/بن و ...) نباید روش اعمال بشه. با فعال کردن «اعمال روی مالک» تو تنظیمات
    گروه، این محافظت خاموش می‌شه - با این حال، خودِ تلگرام هیچ‌وقت اجازه نمی‌ده مالک واقعیِ
    گروه رستریکت/بن بشه (این یه محدودیت پلتفرمه که هیچ ربات یا ادمینی نمی‌تونه دورش بزنه).
    """
    s = get_settings(chat_id)
    if s["admin_actions_on_owner"]:
        return False
    owner_id = await get_group_owner_id(context, chat_id)
    return owner_id is not None and owner_id == target_user_id


# ---- سکوت‌شده‌ها (برای نمایش لیست تو پنل ادمین و همینطور آزادسازی درست) ----
def record_mute(chat_id: int, user_id: int, user_name: str, until_ts, reason: str = ""):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO mutes (chat_id, user_id, user_name, muted_at, until_ts, reason) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET user_name=excluded.user_name, muted_at=excluded.muted_at, "
        "until_ts=excluded.until_ts, reason=excluded.reason",
        (chat_id, user_id, user_name, datetime.utcnow().isoformat(), until_ts, reason),
    )
    conn.commit()
    conn.close()


def remove_mute_record(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def list_active_mutes(chat_id: int):
    """لیست سکوت‌شده‌های فعلی رو برمی‌گردونه؛ اونایی که زمانشون گذشته رو خودکار از دیتابیس پاک می‌کنه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM mutes WHERE chat_id=? ORDER BY until_ts IS NULL DESC, until_ts ASC", (chat_id,))
    rows = c.fetchall()
    now_ts = int(datetime.utcnow().timestamp())
    active, expired_ids = [], []
    for r in rows:
        if r["until_ts"] and r["until_ts"] <= now_ts:
            expired_ids.append(r["user_id"])
        else:
            active.append(r)
    if expired_ids:
        c.executemany("DELETE FROM mutes WHERE chat_id=? AND user_id=?", [(chat_id, uid) for uid in expired_ids])
        conn.commit()
    conn.close()
    return active


# ---- مسدودشده‌ها (برای نمایش لیست تو پنل ادمین و آزادسازی) ------------------
def record_ban(chat_id: int, user_id: int, user_name: str, reason: str = ""):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO banned_users (chat_id, user_id, user_name, banned_at, reason) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET user_name=excluded.user_name, banned_at=excluded.banned_at, "
        "reason=excluded.reason",
        (chat_id, user_id, user_name, datetime.utcnow().isoformat(), reason),
    )
    conn.commit()
    conn.close()


def remove_ban_record(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def list_banned(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM banned_users WHERE chat_id=?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def log_mod_action(*args, **kwargs):
    """دیگه لازم نیست جایی ثبت بشه (پنل حذف شد)؛ این فقط برای سازگاری با فراخوانی‌های قبلیه."""
    pass


# ---------------------------------------------------------------------------
# چالش‌های سرگرمیِ ریاضی + سیستمِ سطح
# ---------------------------------------------------------------------------
CHALLENGE_XP_PER_CORRECT = 15
CHALLENGE_DEFAULT_INTERVAL_SECONDS = 900     # پیش‌فرضِ فاصله‌ی بین چالش‌ها (۱۵ دقیقه)، از پنل قابل تغییره
CHALLENGE_REPOST_INTERVAL_SECONDS = 3600     # هر ۱ ساعت، اگه هنوز جواب داده نشده، پیامش رو تجدید می‌کنیم
CHALLENGE_TOTAL_LIFETIME_SECONDS = 12 * 3600  # هر چالش کلاً ۱۲ ساعت فرصت داره جواب داده بشه
CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS = 30  # جوابِ کاربر (چه درست چه غلط) بینِ این بازه پاک می‌شه
CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS = 60

# ---- سوال‌های ویژه (برای کسایی که لولِ اصلیشون بالای ۵ هست) -----------------
BONUS_MIN_LEVEL = 5                    # لولِ ۵ و بالاتر واجدِ شرایطِ سوالِ ویژه‌ست (شاملِ خودِ ۵)
BONUS_HARD_LEVEL_THRESHOLD = 9         # از این لول به بالا (۹ و ۱۰)، سوال حتی سخت‌تر می‌شه
BONUS_INTERVAL_SECONDS = 12 * 3600     # دقیقاً هر ۱۲ ساعت، نه یه بازه‌ی رندوم
BONUS_XP_PER_CORRECT = 30              # لولِ اصلی که سوالِ ویژه می‌ده (دو برابرِ سوالِ عادی)
BONUS_CHALLENGE_LIFETIME_SECONDS = 12 * 3600  # اگه هدف جواب نده، بعد این‌قدر بی‌سروصدا منقضی می‌شه


CHALLENGE_DIFFICULTIES = ("normal", "hard", "terrifying")
CHALLENGE_DIFFICULTY_LABELS = {"normal": "عادی", "hard": "سخت", "terrifying": "وحشتناک"}


def generate_normal_math_question():
    """یه سوالِ ریاضیِ ساده (جمع/تفریق/ضرب/تقسیم) با اعدادِ حداکثر ۳ رقمی می‌سازه.
    ضرب/تقسیم رو عمداً ساده‌تر نگه می‌داریم که خیلی سخت نشه."""
    op = random.choice(["+", "-", "*", "/"])
    if op == "+":
        a, b = random.randint(1, 999), random.randint(1, 999)
        answer = a + b
    elif op == "-":
        a, b = random.randint(1, 999), random.randint(1, 999)
        if b > a:
            a, b = b, a
        answer = a - b
    elif op == "*":
        a, b = random.randint(2, 50), random.randint(2, 20)
        answer = a * b
    else:  # تقسیم - همیشه یه جوابِ صحیح و تمیز می‌سازیم
        b = random.randint(2, 20)
        answer = random.randint(2, 50)
        a = b * answer
    display_op = {"+": "+", "-": "-", "*": "×", "/": "÷"}[op]
    question_text = f"{a} {display_op} {b}"
    return answer, question_text


def generate_hard_math_question():
    """سطحِ «سخت»: همون جمع/تفریق/ضرب ولی با اعدادِ خیلی بزرگ‌تر، به‌علاوه‌ی رادیکال."""
    op = random.choice(["+", "-", "*", "sqrt"])
    if op == "+":
        a, b = random.randint(500, 9999), random.randint(500, 9999)
        return a + b, f"{a} + {b}"
    if op == "-":
        a, b = random.randint(500, 9999), random.randint(500, 9999)
        if b > a:
            a, b = b, a
        return a - b, f"{a} - {b}"
    if op == "*":
        a, b = random.randint(20, 99), random.randint(10, 50)
        return a * b, f"{a} × {b}"
    # رادیکال: همیشه یه عددِ کامل و تمیز
    root = random.randint(10, 50)
    return root, f"√{root * root}"


def generate_terrifying_math_question():
    """سطحِ «وحشتناک»: رادیکال، انتگرال، کسینوس، فیثاغورث و فیزیک - ولی همیشه با جوابِ عددیِ
    صحیح و تمیز (که چک‌کردنِ خودکارِ جواب همچنان ساده بمونه)."""
    kind = random.choice(["sqrt", "integral", "cosine", "pythagoras", "physics"])

    if kind == "sqrt":
        root = random.randint(20, 80)
        return root, f"√{root * root}"

    if kind == "integral":
        a = random.randint(2, 15)
        if random.random() < 0.5:
            return a * a, f"∫₀^{a} 2x dx"
        return a ** 3, f"∫₀^{a} 3x² dx"

    if kind == "cosine":
        table = {0: 1, 90: 0, 180: -1, 360: 1}
        angle = random.choice(list(table.keys()))
        k = random.randint(2, 50)
        return k * table[angle], f"{k} × cos({angle}°)"

    if kind == "pythagoras":
        triples = [(3, 4, 5), (6, 8, 10), (5, 12, 13), (9, 12, 15), (8, 15, 17), (7, 24, 25), (20, 21, 29), (12, 16, 20)]
        a, b, c = random.choice(triples)
        return c, f"تو یه مثلثِ قائم‌الزاویه، دو ضلع {a} و {b}؛ ضلعِ سوم (وتر)"

    # فیزیک
    if random.random() < 0.5:
        t = random.randint(2, 10)
        v = random.randint(20, 120)
        d = v * t
        return v, f"ماشینی مسافتِ {d} کیلومتر رو تو {t} ساعت رفته؛ سرعتش به km/h (v=d/t)"
    m = random.randint(2, 20)
    accel = random.randint(2, 15)
    return m * accel, f"جرمِ {m} کیلوگرم با شتابِ {accel} m/s²؛ نیرو به نیوتون (F=m×a)"


def generate_math_question(difficulty: str = "normal"):
    if difficulty == "hard":
        return generate_hard_math_question()
    if difficulty == "terrifying":
        return generate_terrifying_math_question()
    return generate_normal_math_question()


def generate_bonus_math_question(user_level: int):
    """سوالِ «ویژه»ی سخت‌تر، مخصوصِ کسایی که لولِ اصلیشون بالای ۵ هست. برای لولِ ۹ و ۱۰ حتی
    سخت‌تر می‌شه (یه حالتِ سطح‌بندیِ جدا از خودِ سوال‌های عادی)."""
    is_hard = user_level >= BONUS_HARD_LEVEL_THRESHOLD
    op = random.choice(["+", "-", "*"])
    if op == "+":
        lo, hi = (200, 999) if is_hard else (100, 700)
        a, b = random.randint(lo, hi), random.randint(lo, hi)
        answer = a + b
    elif op == "-":
        lo, hi = (200, 999) if is_hard else (100, 700)
        a, b = random.randint(lo, hi), random.randint(lo, hi)
        if b > a:
            a, b = b, a
        answer = a - b
    else:  # ضرب - سخت‌ترین حالت، ولی بازم قابلِ حساب کردن
        a_range, b_range = ((20, 99), (10, 30)) if is_hard else ((10, 50), (5, 20))
        a, b = random.randint(*a_range), random.randint(*b_range)
        answer = a * b
    display_op = {"+": "+", "-": "-", "*": "×"}[op]
    question_text = f"{a} {display_op} {b}"
    return answer, question_text


def get_active_challenge(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM active_challenges WHERE chat_id=? AND resolved=0", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_all_active_challenges():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM active_challenges WHERE resolved=0")
    rows = c.fetchall()
    conn.close()
    return rows


def save_active_challenge(chat_id: int, message_id: int, answer: int, question_text: str):
    now_iso = datetime.utcnow().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO active_challenges (chat_id, message_id, answer, question_text, created_at, last_reposted_at, resolved) "
        "VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id, answer=excluded.answer, "
        "question_text=excluded.question_text, created_at=excluded.created_at, "
        "last_reposted_at=excluded.last_reposted_at, resolved=0",
        (chat_id, message_id, answer, question_text, now_iso, now_iso),
    )
    conn.commit()
    conn.close()


def bump_active_challenge(chat_id: int, new_message_id: int):
    """وقتی چالش رو دوباره می‌فرستیم (یادآوریِ ساعتی)، فقط آیدیِ پیام و زمانِ آخرین یادآوری عوض می‌شه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE active_challenges SET message_id=?, last_reposted_at=? WHERE chat_id=?",
        (new_message_id, datetime.utcnow().isoformat(), chat_id),
    )
    conn.commit()
    conn.close()


def resolve_challenge(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE active_challenges SET resolved=1 WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def set_next_challenge_at(chat_id: int, when_ts: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET next_challenge_at=? WHERE chat_id=?", (when_ts, chat_id))
    conn.commit()
    conn.close()


def _parse_iso(text, fallback=None):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return fallback or datetime.utcnow()


async def challenge_maintenance_job(context: ContextTypes.DEFAULT_TYPE):
    """این جاب مسئولِ کلِ چرخه‌ی عمرِ چالش‌های فعاله (مستقل از فرستادنِ چالشِ جدید):
    - اگه از پستِ اولیه‌ی یه چالش ۱۲ ساعت گذشته و هنوز کسی جواب نداده، بی‌سروصدا (بدون هیچ پیامی)
      تمومش می‌کنیم: پیامش پاک می‌شه، resolved می‌شه، و نوبتِ چالشِ بعدی طبقِ فاصله‌ی
      تنظیم‌شده‌ی همون گروه از همین لحظه شمرده می‌شه.
    - وگرنه، اگه از آخرین‌باری که این چالش فرستاده/یادآوری شده ۱ ساعت گذشته، پیامِ قبلی‌ش رو پاک
      می‌کنیم و همون سوال رو دوباره می‌فرستیم (میاد بالای گپ) تا فراموش نشه - تا وقتی یکی جواب بده."""
    bot = context.bot
    for active in get_all_active_challenges():
        chat_id = active["chat_id"]
        created_dt = _parse_iso(active["created_at"])
        last_repost_dt = _parse_iso(active["last_reposted_at"], fallback=created_dt)
        now = datetime.utcnow()

        if (now - created_dt).total_seconds() >= CHALLENGE_TOTAL_LIFETIME_SECONDS:
            resolve_challenge(chat_id)
            try:
                await bot.delete_message(chat_id, active["message_id"])
            except Exception:
                pass
            settings = get_settings(chat_id)
            interval = settings["challenge_interval_seconds"] or CHALLENGE_DEFAULT_INTERVAL_SECONDS
            set_next_challenge_at(chat_id, int(now.timestamp()) + interval)
            continue

        if (now - last_repost_dt).total_seconds() >= CHALLENGE_REPOST_INTERVAL_SECONDS:
            try:
                await bot.delete_message(chat_id, active["message_id"])
            except Exception:
                pass
            text = (
                f"🧮 چالشِ ریاضی! (یادآوری)\n\n"
                f"{active['question_text']} = ?\n\n"
                f"هرکی زودتر رو همینِ پیام ریپلای بزنه و جوابِ درست رو بگه، {CHALLENGE_XP_PER_CORRECT} تا لول می‌گیره! 🏆"
            )
            try:
                sent = await bot.send_message(chat_id, text)
            except Exception:
                continue
            track_message(chat_id, sent.message_id)
            bump_active_challenge(chat_id, sent.message_id)


def add_xp(user_id: int, amount: int):
    """XP رو اضافه می‌کنه و (xp_قدیم, xp_جدید) رو برمی‌گردونه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT xp FROM user_levels WHERE user_id=?", (user_id,))
    row = c.fetchone()
    old_xp = row["xp"] if row else 0
    new_xp = old_xp + amount
    if row is None:
        c.execute("INSERT INTO user_levels (user_id, xp) VALUES (?, ?)", (user_id, new_xp))
    else:
        c.execute("UPDATE user_levels SET xp=? WHERE user_id=?", (new_xp, user_id))
    conn.commit()
    conn.close()
    return old_xp, new_xp


MAIN_LEVEL_CAP = 50           # سقفِ لولِ اصلی (قبلاً ۱۰ بود)
MAIN_LEVEL_XP_PER_LEVEL = 100  # هر ۱۰۰ اکسپی یه سطح؛ برای رسیدن به سقف (۵۰) پس ۵۰۰۰ اکسپی لازمه


def level_from_xp(xp: int):
    """برمی‌گردونه (سطح, آیا_افسانه‌ای). هر ۱۰۰ اکسپی یه سطح، تا سقفِ ۵۰؛ رسیدن به سقف یعنی «افسانه‌ای»."""
    legendary_threshold = MAIN_LEVEL_CAP * MAIN_LEVEL_XP_PER_LEVEL
    if xp >= legendary_threshold:
        return MAIN_LEVEL_CAP, True
    return 1 + (xp // MAIN_LEVEL_XP_PER_LEVEL), False


def add_subject_correct(user_id: int, subject: str):
    """هر جوابِ درستِ یه درس، دقیقاً ۱ امتیازِ مثبت ثبت می‌کنه (فرقی نمی‌کنه سوالِ عادی بوده یا
    ویژه). سطحِ اون درس هم مستقیماً از رو همین مجموع حساب می‌شه: هر ۱۰ تا مثبت، یه سطح."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT correct_count FROM user_subject_levels WHERE user_id=? AND subject=?", (user_id, subject))
    row = c.fetchone()
    old_count = row["correct_count"] if row else 0
    new_count = old_count + 1
    if row is None:
        c.execute(
            "INSERT INTO user_subject_levels (user_id, subject, correct_count, points) VALUES (?, ?, ?, ?)",
            (user_id, subject, new_count, new_count),
        )
    else:
        c.execute(
            "UPDATE user_subject_levels SET correct_count=?, points=? WHERE user_id=? AND subject=?",
            (new_count, new_count, user_id, subject),
        )
    conn.commit()
    conn.close()
    return new_count


def subject_level_from_count(count: int) -> int:
    """هر ۱۰ تا جوابِ درست، یه سطح؛ حداکثر سطح ۱۰ (۱۰۰ تا جوابِ درست)."""
    return min(10, 1 + (count // 10))


def get_math_leaderboard(chat_id: int, limit: int = 10):
    """۱۰ نفرِ برترِ ریاضیِ یه گروهِ خاص (فقط اعضایی که الان تو گروه حضور دارن و حداقل یه‌بار
    جوابِ درست دادن)، به‌ترتیبِ تعدادِ جوابِ درست، به‌همراهِ لولِ کلی و سطحِ درسِ ریاضیشون."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT gm.user_id, gm.first_name, "
        "COALESCE(usl.correct_count, 0) AS correct_count, "
        "COALESCE(usl.points, 0) AS subject_points, "
        "COALESCE(ul.xp, 0) AS xp "
        "FROM group_members gm "
        "LEFT JOIN user_subject_levels usl ON usl.user_id = gm.user_id AND usl.subject = ? "
        "LEFT JOIN user_levels ul ON ul.user_id = gm.user_id "
        "WHERE gm.chat_id = ? AND gm.is_present = 1 AND COALESCE(usl.correct_count, 0) > 0 "
        "ORDER BY correct_count DESC LIMIT ?",
        ("ریاضی", chat_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


LEADERBOARD_MEDALS = ("🥇", "🥈", "🥉")


async def math_leaderboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی رو یه پیامِ خودِ ربات ریپلای بزنی و بنویسی «برترین ریاضی»، ۱۰ نفرِ برترِ ریاضیِ همین
    گروه رو نشون می‌ده: چندتا جواب درست دادن، لولِ کلیشون، و سطحِ درسِ ریاضیشون."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    msg = update.message
    if msg is None or msg.reply_to_message is None or not msg.text:
        return
    if msg.reply_to_message.from_user is None or msg.reply_to_message.from_user.id != context.bot.id:
        return
    if msg.text.strip() != "برترین ریاضی":
        return

    rows = get_math_leaderboard(chat.id, limit=10)
    if not rows:
        await reply_tracked(msg, "هنوز هیچ‌کس تو این گروه به چالشِ ریاضی جوابِ درست نداده!")
        return

    blocks = ["🏆 برترین‌های ریاضیِ این گروه\n"]
    for i, row in enumerate(rows, start=1):
        prefix = LEADERBOARD_MEDALS[i - 1] if i <= 3 else f"{i}."
        name_escaped = html.escape(row["first_name"] or "یه بازیکن")
        mention = f'<a href="tg://user?id={row["user_id"]}">{name_escaped}</a>'
        overall_level, is_legendary = level_from_xp(row["xp"])
        overall_label = "افسانه‌ای 👑" if is_legendary else f"سطح {overall_level}"
        math_level = subject_level_from_count(row["correct_count"])
        blocks.append(
            f"{prefix} {mention}\n"
            f"   ▫️ جواب داده‌ها: {row['correct_count']}\n"
            f"   ▫️ لولِ اصلی: {overall_label}\n"
            f"   ▫️ لولِ ریاضی: سطح {math_level}"
        )

    await reply_tracked(msg, "\n\n".join(blocks), parse_mode=ParseMode.HTML)


async def challenge_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """پیامِ چالش و پیامِ جوابِ کاربر رو پاک می‌کنه تا گپ تمیز بمونه."""
    data = context.job.data
    chat_id = data["chat_id"]
    for mid in data["message_ids"]:
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def post_new_challenge(bot, chat_id: int, difficulty: str = "normal") -> bool:
    """اگه چالشِ حل‌نشده‌ای از قبل تو این گروه نباشه، یه چالشِ ریاضیِ جدید می‌فرسته.
    نگه‌داشتنِ چالشِ فعال (یادآوریِ ساعتی، انقضای ۱۲ ساعته) کاملاً جدا و توسطِ
    challenge_maintenance_job انجام می‌شه، نه این تابع."""
    if get_active_challenge(chat_id) is not None:
        return False
    try:
        answer, question_text = generate_math_question(difficulty)
    except Exception as e:
        logger.warning(f"⚠️ تولیدِ سوالِ سختیِ «{difficulty}» خطا داد ({e})؛ رفتیم رو سوالِ عادی تا لااقل چالش بی‌جواب نمونه.")
        answer, question_text = generate_normal_math_question()
        difficulty = "normal"
    difficulty_line = f"سطح: {CHALLENGE_DIFFICULTY_LABELS.get(difficulty, 'عادی')} 🔥\n\n" if difficulty != "normal" else ""
    text = (
        f"🧮 چالشِ ریاضی!\n\n"
        f"{difficulty_line}"
        f"{question_text} = ?\n\n"
        f"هرکی زودتر رو همینِ پیام ریپلای بزنه و جوابِ درست رو بگه، {CHALLENGE_XP_PER_CORRECT} تا لول می‌گیره! 🏆"
    )
    try:
        sent = await bot.send_message(chat_id, text)
    except Exception:
        return False
    track_message(chat_id, sent.message_id)
    save_active_challenge(chat_id, sent.message_id, answer, question_text)
    return True


async def maybe_post_challenge_for_chat(bot, chat_id: int):
    """چک می‌کنه آیا الان نوبتِ چالشِ بعدیِ این گروهه؛ اگه بله و چالشِ فعالِ دیگه‌ای در جریان
    نباشه، یه چالشِ جدید می‌فرسته و نوبتِ بعدی رو طبقِ فاصله‌ی تنظیم‌شده‌ی همون گروه می‌ذاره.
    اگه یه چالش همین الان فعاله، اصلاً کاری نمی‌کنه (نه چیزی می‌فرسته، نه next_challenge_at رو
    دست می‌زنه) - نگه‌داشتنِ چالشِ فعال به‌طور کامل کارِ challenge_maintenance_job ـه، این یعنی
    دیگه لازم نیست این تابع هر ۶۰ ثانیه یه بار برای ساعت‌ها بی‌خودی دیتابیس رو آپدیت کنه.
    این تابع از دو تا مسیرِ کاملاً مستقل صدا زده می‌شه: هم از جاب‌کیو، هم از دلِ پردازشِ
    پیام‌های عادیِ گروه - تا حتی اگه جاب‌کیوِ سرور فعال نباشه، چالش‌ها بازم سرِ وقت برسن."""
    settings = get_settings(chat_id)
    if not settings["challenges_enabled"]:
        return
    if get_active_challenge(chat_id) is not None:
        return
    now_ts = int(datetime.utcnow().timestamp())
    next_at = settings["next_challenge_at"] or 0
    if now_ts < next_at:
        return
    posted = await post_new_challenge(bot, chat_id, difficulty=settings["challenge_difficulty"] or "normal")
    if posted:
        interval = settings["challenge_interval_seconds"] or CHALLENGE_DEFAULT_INTERVAL_SECONDS
        set_next_challenge_at(chat_id, now_ts + interval)


async def challenge_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    """مسیرِ اصلیِ زمان‌بندی: هر ۶۰ ثانیه چک می‌کنه کدوم گروه‌ها نوبتِ چالشِ بعدیشونه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM group_settings WHERE challenges_enabled=1")
    chat_ids = [row["chat_id"] for row in c.fetchall()]
    conn.close()
    for chat_id in chat_ids:
        try:
            await maybe_post_challenge_for_chat(context.bot, chat_id)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# سوال‌های ویژه: دقیقاً هر ۱۲ ساعت، به همه‌ی کسایی که لولِ اصلیشون ۵ یا بالاتره (تک‌تکشون، نه
# فقط یه نفرِ رندوم)، یه سوالِ سخت‌تر داده می‌شه. جوابِ درست ۳۰ لولِ اصلی و ۲۰ امتیازِ درسِ
# ریاضی می‌ده (دو برابرِ سوالِ عادی).
# ---------------------------------------------------------------------------
def get_active_bonus_challenge(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM active_bonus_challenges WHERE chat_id=? AND user_id=? AND resolved=0", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row


def get_any_active_bonus_challenge(chat_id: int):
    """آیا همین الان تو این گروه، برای هرکسی، یه سوالِ ویژه‌ی حل‌نشده در جریانه؟ (می‌خوایم
    هم‌زمان فقط یه نفر سوالِ ویژه بگیره، نه چندتا با هم)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM active_bonus_challenges WHERE chat_id=? AND resolved=0 LIMIT 1", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_active_bonus_challenge_by_message(chat_id: int, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM active_bonus_challenges WHERE chat_id=? AND message_id=? AND resolved=0", (chat_id, message_id)
    )
    row = c.fetchone()
    conn.close()
    return row


def get_all_active_bonus_challenges():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM active_bonus_challenges WHERE resolved=0")
    rows = c.fetchall()
    conn.close()
    return rows


def save_active_bonus_challenge(chat_id: int, user_id: int, message_id: int, answer: int, question_text: str):
    """نکته‌ی مهم (رفعِ یه باگِ واقعی): قبلاً اینجا از «INSERT OR REPLACE» استفاده شده بود که
    فقط مخصوصِ SQLite ـه و رو PostgreSQL (که طبقِ همون داکیومنتِ پروژه، حالتِ پیش‌فرضِ ربات
    روی سرورِ واقعیه) اصلاً اجرا نمی‌شه - یعنی این تابع همیشه شکست می‌خورد، پیامِ سوال
    فرستاده می‌شد ولی هیچ‌وقت تو دیتابیس ثبت نمی‌شد، پس جوابِ کاربر هیچ‌وقت پیدا نمی‌شد.
    راه‌حلِ درست: به‌جای هر نوع syntax مخصوصِ یه دیتابیس (چه INSERT OR REPLACE چه ON CONFLICT
    با نامِ دقیقِ ستون‌ها)، دستی اول هرچی برای این گروه موجوده رو پاک می‌کنیم، بعد ردیفِ تازه رو
    اضافه می‌کنیم. این با SQLite و PostgreSQL هر دو، و با هر شکلی از کلیدِ جدول (قدیم یا جدید)
    درست کار می‌کنه، چون فقط از DELETE و INSERT سادهٔ استاندارد استفاده می‌کنه."""
    now_iso = datetime.utcnow().isoformat()
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM active_bonus_challenges WHERE chat_id=?", (chat_id,))
        c.execute(
            "INSERT INTO active_bonus_challenges "
            "(chat_id, user_id, message_id, answer, question_text, created_at, resolved) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (chat_id, user_id, message_id, answer, question_text, now_iso),
        )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"⚠️ نتونستم چالشِ ویژه رو تو دیتابیس ذخیره کنم: {e}")
    conn.close()


def resolve_bonus_challenge(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE active_bonus_challenges SET resolved=1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def set_next_bonus_challenge_at(chat_id: int, when_ts: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET next_bonus_challenge_at=? WHERE chat_id=?", (when_ts, chat_id))
    conn.commit()
    conn.close()


def _get_bonus_eligible_members(chat_id: int):
    """همه‌ی اعضای حاضرِ گروه که لولِ اصلیشون (بر اساسِ XPِ واقعیِ همین لحظه، نه یه مقدارِ
    کش‌شده) دقیقاً ۵ یا بالاتره رو برمی‌گردونه: لیستی از (user_id, first_name, level)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT gm.user_id, gm.first_name, ul.xp "
        "FROM group_members gm JOIN user_levels ul ON ul.user_id = gm.user_id "
        "WHERE gm.chat_id = ? AND gm.is_present = 1 AND ul.xp > 0",
        (chat_id,),
    )
    rows = c.fetchall()
    conn.close()
    eligible = []
    for row in rows:
        level, _ = level_from_xp(row["xp"])
        if level >= BONUS_MIN_LEVEL:
            eligible.append((row["user_id"], row["first_name"], level))
    return eligible


async def post_bonus_challenge_for_user(bot, chat_id: int, user_id: int, first_name: str, level: int) -> bool:
    answer, question_text = generate_bonus_math_question(level)
    name_escaped = html.escape(first_name or "بازیکن")
    mention = f'<a href="tg://user?id={user_id}">{name_escaped}</a>'
    text = (
        f"🎯 سوالِ ویژه برای {mention}!\n"
        f"چون سطحت {BONUS_MIN_LEVEL} یا بالاتره، یه سوالِ سخت‌تر برات داریم:\n\n"
        f"{question_text} = ?\n\n"
        f"جوابِ درست {BONUS_XP_PER_CORRECT} تا لولِ اصلی می‌ده (دو برابرِ سوالِ عادی)! "
        f"فقط خودِ {name_escaped} می‌تونه رو این پیام ریپلای بزنه و جواب بده."
    )
    try:
        sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        return False
    track_message(chat_id, sent.message_id)
    save_active_bonus_challenge(chat_id, user_id, sent.message_id, answer, question_text)
    return True


async def maybe_post_bonus_challenge_for_chat(bot, chat_id: int):
    """اگه نوبتِ این گروه رسیده باشه و همین الان سوالِ ویژه‌ی حل‌نشده‌ای در جریان نباشه، از بینِ
    اعضای واجدِ شرایط (لولِ ۵ یا بالاتر) فقط یکی رو رندوم انتخاب می‌کنه (نه همه‌شون با هم)."""
    settings = get_settings(chat_id)
    if not settings["challenges_enabled"]:
        return
    now_ts = int(datetime.utcnow().timestamp())
    next_at = settings["next_bonus_challenge_at"] or 0
    if now_ts < next_at:
        return
    if get_any_active_bonus_challenge(chat_id) is None:
        eligible = _get_bonus_eligible_members(chat_id)
        if eligible:
            user_id, first_name, level = random.choice(eligible)
            try:
                await post_bonus_challenge_for_user(bot, chat_id, user_id, first_name, level)
            except Exception:
                pass
    set_next_bonus_challenge_at(chat_id, now_ts + BONUS_INTERVAL_SECONDS)


async def bonus_challenge_maintenance_job(context: ContextTypes.DEFAULT_TYPE):
    """اگه یه سوالِ ویژه ۱۲ ساعت بی‌جواب بمونه، بی‌سروصدا (بدونِ هیچ پیامی) پاک و بسته می‌شه."""
    now = datetime.utcnow()
    for active in get_all_active_bonus_challenges():
        created_dt = _parse_iso(active["created_at"])
        if (now - created_dt).total_seconds() >= BONUS_CHALLENGE_LIFETIME_SECONDS:
            resolve_bonus_challenge(active["chat_id"], active["user_id"])
            try:
                await context.bot.delete_message(active["chat_id"], active["message_id"])
            except Exception:
                pass


async def bonus_challenge_scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    """هر ۳۰ دقیقه چک می‌کنه کدوم گروه‌ها نوبتِ سوالِ ویژه‌ی بعدیشونه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM group_settings WHERE challenges_enabled=1")
    chat_ids = [row["chat_id"] for row in c.fetchall()]
    conn.close()
    for chat_id in chat_ids:
        try:
            await maybe_post_bonus_challenge_for_chat(context.bot, chat_id)
        except Exception:
            continue


async def bonus_challenge_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فقط خودِ فردی که سوالِ ویژه براش پرسیده شده می‌تونه جواب بده؛ بقیه‌ی ریپلای‌ها نادیده
    گرفته می‌شن (برای اینکه گپ شلوغ نشه با پیام‌های «این سوالِ تو نبود»)."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    msg = update.message
    if msg is None or msg.reply_to_message is None or not msg.text:
        return
    chat_id = chat.id
    active = get_active_bonus_challenge_by_message(chat_id, msg.reply_to_message.message_id)
    if active is None:
        return
    if update.effective_user.id != active["user_id"]:
        return

    cleaned = msg.text.strip().replace(",", "").replace("،", "").replace(" ", "")
    try:
        given = int(cleaned)
    except ValueError:
        wrong_msg = await reply_tracked(msg, "❌ این که عدد نیست! جوابِ سوالِ ویژه هم باید یه عددِ ساده باشه.")
        if context.job_queue is not None:
            context.job_queue.run_once(
                challenge_cleanup_job,
                random.randint(CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS, CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS),
                data={"chat_id": chat_id, "message_ids": [wrong_msg.message_id, msg.message_id]},
            )
        return
    if given != active["answer"]:
        wrong_msg = await reply_tracked(msg, "❌ جواب غلط بود، یه بار دیگه امتحان کن!")
        if context.job_queue is not None:
            context.job_queue.run_once(
                challenge_cleanup_job,
                random.randint(CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS, CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS),
                data={"chat_id": chat_id, "message_ids": [wrong_msg.message_id, msg.message_id]},
            )
        return

    resolve_bonus_challenge(chat_id, active["user_id"])
    user = update.effective_user
    old_xp, new_xp = add_xp(user.id, BONUS_XP_PER_CORRECT)
    old_level, _ = level_from_xp(old_xp)
    new_level, is_legendary = level_from_xp(new_xp)
    new_math_count = add_subject_correct(user.id, "ریاضی")
    math_level = subject_level_from_count(new_math_count)

    name_escaped = html.escape(user.first_name or "یه بازیکن")
    mention = f'<a href="tg://user?id={user.id}">{name_escaped}</a>'
    result_text = (
        f"🎯 سوالِ ویژه!\n\n"
        f"✅ {mention} جوابِ درست رو داد و {BONUS_XP_PER_CORRECT} تا لولِ اصلی گرفت! 🏆\n\n"
        f"<blockquote>ریاضی: سطح {math_level}</blockquote>\n\n"
        f"جوابِ سوال: {active['answer']}"
    )
    try:
        await context.bot.edit_message_text(result_text, chat_id=chat_id, message_id=active["message_id"], parse_mode=ParseMode.HTML)
    except Exception:
        pass

    if context.job_queue is not None:
        context.job_queue.run_once(
            challenge_cleanup_job,
            random.randint(CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS, CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS),
            data={"chat_id": chat_id, "message_ids": [active["message_id"], msg.message_id]},
        )

    if new_level > old_level:
        levelup_text = level_up_message_text(mention, old_level, new_level, is_legendary)
        try:
            sent_lvl = await context.bot.send_message(chat_id, levelup_text, parse_mode=ParseMode.HTML)
            track_message(chat_id, sent_lvl.message_id)
        except Exception:
            pass


async def challenge_time_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مسیرِ پشتیبان: با هر پیامِ عادیِ گروه هم چک می‌کنه نوبتِ چالش رسیده یا نه. این یعنی حتی
    اگه جاب‌کیوِ ربات (به هر دلیلی) کار نکنه، همین‌که تو گروه پیام رد و بدل بشه، چالش‌ها باز هم
    سرِ وقت میان."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    try:
        await maybe_post_challenge_for_chat(context.bot, chat.id)
    except Exception:
        pass


def level_up_message_text(mention: str, old_level: int, new_level: int, is_legendary: bool) -> str:
    if is_legendary:
        return f"👑🔥 {mention} به مقامِ افسانه‌ای رسید! 👑"
    return f"🎉 لولِ {mention} به سطح {new_level} ارتقا پیدا کرد!"


async def challenge_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اگه این پیام ریپلای به یه چالشِ فعاله و جواب درسته، چالش رو می‌بنده، ادیتش می‌کنه، سطح‌بندی
    رو به‌روز می‌کنه و بعد از ۵ دقیقه پیام‌ها رو خودکار پاک می‌کنه."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    msg = update.message
    if msg is None or msg.reply_to_message is None or not msg.text:
        return
    chat_id = chat.id
    active = get_active_challenge(chat_id)
    if active is None or msg.reply_to_message.message_id != active["message_id"]:
        return

    cleaned = msg.text.strip().replace(",", "").replace("،", "").replace(" ", "")
    try:
        given = int(cleaned)
    except ValueError:
        wrong_msg = await reply_tracked(msg, "❌ این که عدد نیست! جوابِ چالش باید یه عددِ ساده باشه.")
        if context.job_queue is not None:
            context.job_queue.run_once(
                challenge_cleanup_job,
                random.randint(CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS, CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS),
                data={"chat_id": chat_id, "message_ids": [wrong_msg.message_id, msg.message_id]},
            )
        return
    if given != active["answer"]:
        wrong_msg = await reply_tracked(msg, "❌ جواب شما غلط بود، یه بار دیگه امتحان کن!")
        if context.job_queue is not None:
            context.job_queue.run_once(
                challenge_cleanup_job,
                random.randint(CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS, CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS),
                data={"chat_id": chat_id, "message_ids": [wrong_msg.message_id, msg.message_id]},
            )
        return

    resolve_challenge(chat_id)
    user = update.effective_user
    old_xp, new_xp = add_xp(user.id, CHALLENGE_XP_PER_CORRECT)
    old_level, _ = level_from_xp(old_xp)
    new_level, is_legendary = level_from_xp(new_xp)
    new_math_count = add_subject_correct(user.id, "ریاضی")
    math_level = subject_level_from_count(new_math_count)

    name_escaped = html.escape(user.first_name or "یه بازیکن")
    mention = f'<a href="tg://user?id={user.id}">{name_escaped}</a>'
    result_text = (
        f"🧮 چالشِ ریاضی!\n\n"
        f"✅ {mention} جوابِ درست رو داد و {CHALLENGE_XP_PER_CORRECT} تا لول گرفت! 🏆\n\n"
        f"<blockquote>ریاضی: سطح {math_level}</blockquote>\n\n"
        f"جوابِ سوال: {active['answer']}"
    )
    try:
        await context.bot.edit_message_text(result_text, chat_id=chat_id, message_id=active["message_id"], parse_mode=ParseMode.HTML)
    except Exception:
        pass

    if context.job_queue is not None:
        context.job_queue.run_once(
            challenge_cleanup_job,
            random.randint(CHALLENGE_ANSWER_CLEANUP_MIN_SECONDS, CHALLENGE_ANSWER_CLEANUP_MAX_SECONDS),
            data={"chat_id": chat_id, "message_ids": [active["message_id"], msg.message_id]},
        )

    if new_level > old_level:
        levelup_text = level_up_message_text(mention, old_level, new_level, is_legendary)
        try:
            sent_lvl = await context.bot.send_message(chat_id, levelup_text, parse_mode=ParseMode.HTML)
            track_message(chat_id, sent_lvl.message_id)
        except Exception:
            pass


# ---- لقب (هر ادمین می‌تونه برای یه عضو تو همون گروه یه لقب بذاره) ----------
def set_nickname(chat_id: int, user_id: int, nickname: str, set_by: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO nicknames (chat_id, user_id, nickname, set_by, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET nickname=excluded.nickname, set_by=excluded.set_by, created_at=excluded.created_at",
        (chat_id, user_id, nickname, set_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def remove_nickname(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM nicknames WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_nickname(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT nickname FROM nicknames WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row["nickname"] if row else None


def display_name(chat_id: int, user) -> str:
    """اسمی که تو پیام‌های ربات نشون داده می‌شه: اگه لقب داشته باشه همون، وگرنه اسم کوچیکش."""
    nick = get_nickname(chat_id, user.id)
    return nick or (user.first_name or "کاربر")


# ---- کلمات یاد گرفته شده (هر ادمین لیست خصوصیِ خودش رو داره) ---------------
def add_learned_word(chat_id: int, teacher_id: int, trigger_word: str, answer: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(MAX(code), 0) + 1 FROM learned_words WHERE chat_id=? AND teacher_id=?",
        (chat_id, teacher_id),
    )
    new_code = c.fetchone()[0]
    c.execute(
        "INSERT INTO learned_words (chat_id, teacher_id, code, trigger_word, answers) VALUES (?, ?, ?, ?, ?)",
        (chat_id, teacher_id, new_code, trigger_word.strip(), json.dumps([answer.strip()], ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return new_code


def list_learned_words(chat_id: int, teacher_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM learned_words WHERE chat_id=? AND teacher_id=? ORDER BY code",
        (chat_id, teacher_id),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_learned_word(chat_id: int, teacher_id: int, code: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM learned_words WHERE chat_id=? AND teacher_id=? AND code=?",
        (chat_id, teacher_id, code),
    )
    row = c.fetchone()
    conn.close()
    return row


def set_word_answer(chat_id: int, teacher_id: int, code: int, new_answer: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE learned_words SET answers=? WHERE chat_id=? AND teacher_id=? AND code=?",
        (json.dumps([new_answer.strip()], ensure_ascii=False), chat_id, teacher_id, code),
    )
    conn.commit()
    conn.close()


def add_extra_answer(chat_id: int, teacher_id: int, code: int, extra_answer: str):
    row = get_learned_word(chat_id, teacher_id, code)
    if row is None:
        return
    answers = json.loads(row["answers"])
    answers.append(extra_answer.strip())
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE learned_words SET answers=? WHERE chat_id=? AND teacher_id=? AND code=?",
        (json.dumps(answers, ensure_ascii=False), chat_id, teacher_id, code),
    )
    conn.commit()
    conn.close()


def delete_learned_word(chat_id: int, teacher_id: int, code: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM learned_words WHERE chat_id=? AND teacher_id=? AND code=?",
        (chat_id, teacher_id, code),
    )
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def find_matching_word(chat_id: int, teacher_id: int, text: str):
    """
    اگه یکی از کلمات یاد گرفته شده‌ی همون معلم (مثلا مالک گروه) هرجایی داخل متن پیام باشه
    (نه فقط وقتی پیام دقیقاً همون کلمه باشه)، جواب رندوم برمی‌گردونه. اگه چندتا کلمه هم‌زمان
    تو پیام پیدا بشن، اونی که طولانی‌تره (دقیق‌تره) در اولویته.
    """
    text_norm = text.strip()
    if not text_norm:
        return None
    best_row = None
    best_len = -1
    for row in list_learned_words(chat_id, teacher_id):
        trig = row["trigger_word"].strip()
        if trig and trig in text_norm and len(trig) > best_len:
            best_row = row
            best_len = len(trig)
    if best_row:
        answers = json.loads(best_row["answers"])
        if answers:
            return random.choice(answers)
    return None


# ---- سوپرادمین (تو): تو هر گروهی که ربات عضوشه دسترسی کامل داره -------------
def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


# ---- مالک گروه (برای اینکه فقط ادمین‌ها بتونن لیست خصوصی کلمه بسازن) --------
async def is_user_owner(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status == ChatMemberStatus.OWNER
    except Exception:
        return False


# ---- ادمین‌ها -------------------------------------------------------------
async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> bool:
    """ادمین یعنی: سوپرادمین ربات، ادمین/مالک واقعی گروه در تلگرام، یا کسی که دستی اضافه شده."""
    if is_super_admin(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return True
    except Exception:
        pass

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row is not None


def add_manual_admin(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO admins (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
    conn.commit()
    conn.close()


# ---- اخطارها ---------------------------------------------------------------
def add_warning(chat_id: int, user_id: int, reason: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT count, reasons FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    if row is None:
        count = 1
        reasons = [reason]
        c.execute(
            "INSERT INTO warnings (chat_id, user_id, count, reasons) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, count, json.dumps(reasons, ensure_ascii=False)),
        )
    else:
        count = row["count"] + 1
        reasons = json.loads(row["reasons"])
        reasons.append(reason)
        c.execute(
            "UPDATE warnings SET count=?, reasons=? WHERE chat_id=? AND user_id=?",
            (count, json.dumps(reasons, ensure_ascii=False), chat_id, user_id),
        )
    conn.commit()
    conn.close()
    return count


def remove_all_warnings(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_warning_reasons(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT reasons FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    if row is None:
        return []
    return json.loads(row["reasons"])


# ---- کلمات ممنوعه ----------------------------------------------------------
def add_forbidden_word(chat_id: int, word: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO forbidden_words (chat_id, word) VALUES (?, ?)", (chat_id, word.strip()))
    conn.commit()
    conn.close()


def remove_forbidden_word(chat_id: int, word: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM forbidden_words WHERE chat_id=? AND word=?", (chat_id, word.strip()))
    conn.commit()
    conn.close()


def list_forbidden_words(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT word FROM forbidden_words WHERE chat_id=?", (chat_id,))
    rows = [r["word"] for r in c.fetchall()]
    conn.close()
    return rows


def contains_forbidden_word(chat_id: int, text: str):
    text_norm = (text or "").strip()
    if not text_norm:
        return None
    for w in list_forbidden_words(chat_id):
        if w and w in text_norm:
            return w
    return None


# ---- اعضای ویژه -------------------------------------------------------------
def add_special_member(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO special_members (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
    conn.commit()
    conn.close()


def list_special_members(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM special_members WHERE chat_id=?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_special_member(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM special_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row


def is_special_member(chat_id: int, user_id: int) -> bool:
    return get_special_member(chat_id, user_id) is not None


def remove_special_member(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM special_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def set_special_tone(chat_id: int, user_id: int, tone_text: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE special_members SET custom_tone=? WHERE chat_id=? AND user_id=?",
        (tone_text, chat_id, user_id),
    )
    conn.commit()
    conn.close()


def set_special_mode(chat_id: int, user_id: int, mode: str):
    """mode: 'all' (معافیت کامل) یا 'custom' (دستی، بر اساس ستون‌های exempt_*)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE special_members SET mode=? WHERE chat_id=? AND user_id=?", (mode, chat_id, user_id))
    conn.commit()
    conn.close()


# نگاشتِ اسمِ فیچر به اسمِ ستونِ دیتابیس (برای تنظیم دستیِ افراد ویژه)
SPECIAL_FEATURE_COLUMNS = {
    "photo": "exempt_photo",
    "video": "exempt_video",
    "sticker": "exempt_sticker",
    "forward": "exempt_forward",
    "links": "exempt_links",
    "words": "exempt_words",
    "spam": "exempt_spam",
}
SPECIAL_FEATURE_LABELS = {
    "photo": "عکس",
    "video": "فیلم",
    "sticker": "استیکر و گیف",
    "forward": "فوروارد",
    "links": "لینک",
    "words": "کلمات ممنوعه",
    "spam": "اسپم",
}


def toggle_special_feature(chat_id: int, user_id: int, feature: str):
    column = SPECIAL_FEATURE_COLUMNS[feature]
    row = get_special_member(chat_id, user_id)
    if row is None:
        return
    new_val = 0 if row[column] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE special_members SET {column}=? WHERE chat_id=? AND user_id=?", (new_val, chat_id, user_id))
    conn.commit()
    conn.close()


def is_special_exempt(chat_id: int, user_id: int, feature: str) -> bool:
    """آیا این کاربر (اگه عضو ویژه‌ست) از محدودیتِ این فیچر خاص معافه؟"""
    row = get_special_member(chat_id, user_id)
    if row is None:
        return False
    if row["mode"] == "custom":
        return bool(row[SPECIAL_FEATURE_COLUMNS[feature]])
    return True  # حالت پیش‌فرض 'all' یعنی معافیت کامل


# ---- ردیابی پیام‌ها (برای قابلیت حذف پیام‌ها) -------------------------------
def track_message(chat_id: int, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO tracked_messages (chat_id, message_id, created_at) VALUES (?, ?, ?)",
        (chat_id, message_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    # حفظ حجم دیتابیس: فقط آخرین MESSAGES_TO_KEEP پیام هر گروه نگه داشته می‌شه
    c.execute(
        """
        DELETE FROM tracked_messages
        WHERE chat_id=? AND message_id NOT IN (
            SELECT message_id FROM tracked_messages WHERE chat_id=?
            ORDER BY message_id DESC LIMIT ?
        )
        """,
        (chat_id, chat_id, MESSAGES_TO_KEEP),
    )
    conn.commit()
    conn.close()


# ---- آمار پیام‌های گروه (برای دستورِ «آمار») -------------------------------
def record_message_stat(chat_id: int, user_id: int, user_name: str, msg):
    if msg.photo:
        col = "photo_count"
    elif msg.video:
        col = "video_count"
    elif msg.sticker:
        col = "sticker_count"
    elif msg.animation:
        col = "gif_count"
    elif msg.text:
        col = "text_count"
    else:
        col = "other_count"
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            f"INSERT INTO message_stats (chat_id, user_id, user_name, {col}) VALUES (?, ?, ?, 1) "
            f"ON CONFLICT(chat_id, user_id) DO UPDATE SET user_name=excluded.user_name, {col}={col}+1",
            (chat_id, user_id, user_name or "کاربر"),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"⚠️ ثبتِ آمارِ پیام برای چت {chat_id} شکست خورد: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def get_group_message_stats(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "SELECT COALESCE(SUM(text_count),0) AS t, COALESCE(SUM(photo_count),0) AS p, "
            "COALESCE(SUM(video_count),0) AS v, COALESCE(SUM(sticker_count),0) AS s, "
            "COALESCE(SUM(gif_count),0) AS g, COALESCE(SUM(other_count),0) AS o "
            "FROM message_stats WHERE chat_id=?",
            (chat_id,),
        )
        totals = c.fetchone()
        c.execute(
            "SELECT user_name, (text_count+photo_count+video_count+sticker_count+gif_count+other_count) AS total "
            "FROM message_stats WHERE chat_id=? ORDER BY total DESC LIMIT 3",
            (chat_id,),
        )
        top3 = c.fetchall()
        return totals, top3
    except Exception as e:
        logger.warning(f"⚠️ خوندنِ آمارِ گروه {chat_id} شکست خورد: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return {"t": 0, "p": 0, "v": 0, "s": 0, "g": 0, "o": 0}, []


# ---- خاموش/روشن کردنِ ربات برای یه گروهِ خاص --------------------------------
def set_bot_paused(chat_id: int, paused: bool):
    get_settings(chat_id)  # مطمئن می‌شه ردیفِ تنظیمات وجود داره
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET bot_paused=? WHERE chat_id=?", (1 if paused else 0, chat_id))
    conn.commit()


def get_tracked_messages(chat_id: int, limit: int = None):
    conn = get_conn()
    c = conn.cursor()
    if limit:
        c.execute(
            "SELECT message_id FROM tracked_messages WHERE chat_id=? ORDER BY message_id DESC LIMIT ?",
            (chat_id, limit),
        )
    else:
        c.execute("SELECT message_id FROM tracked_messages WHERE chat_id=? ORDER BY message_id DESC", (chat_id,))
    rows = [r["message_id"] for r in c.fetchall()]
    conn.close()
    return rows


def clear_tracked_messages(chat_id: int, message_ids):
    conn = get_conn()
    c = conn.cursor()
    c.executemany(
        "DELETE FROM tracked_messages WHERE chat_id=? AND message_id=?",
        [(chat_id, mid) for mid in message_ids],
    )
    conn.commit()
    conn.close()


# ---- ارسال پیام + ردیابی خودکار (تا «حذف پیام‌ها» پیام‌های خودِ ربات رو هم پاک کنه) ---
async def send_tracked(bot, chat_id: int, text: str, **kwargs):
    sent = await bot.send_message(chat_id, text, **kwargs)
    track_message(chat_id, sent.message_id)
    return sent


async def reply_tracked(message, text: str, **kwargs):
    sent = await message.reply_text(text, **kwargs)
    track_message(message.chat_id, sent.message_id)
    return sent


# ---- اعضای شناخته‌شده‌ی هر گروه (برای تگ همگانی، یادآوری غیبت، و شناخت کاربر برگشتی) ----
def upsert_group_member(chat_id: int, user):
    if user is None or user.is_bot:
        return
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT first_seen FROM group_members WHERE chat_id=? AND user_id=?", (chat_id, user.id))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO group_members (chat_id, user_id, first_name, username, updated_at, first_seen, is_present) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (chat_id, user.id, user.first_name or "کاربر", user.username, now, now),
        )
    else:
        c.execute(
            "UPDATE group_members SET first_name=?, username=?, updated_at=?, is_present=1 "
            "WHERE chat_id=? AND user_id=?",
            (user.first_name or "کاربر", user.username, now, chat_id, user.id),
        )
    conn.commit()
    conn.close()


def get_group_members(chat_id: int):
    """فقط اعضایی که الان تو گروه حضور دارن (برای تگ همگانی و یادآوری غیبت)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, first_name, username FROM group_members WHERE chat_id=? AND is_present=1", (chat_id,)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_member_record(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM group_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row


def mark_member_left(chat_id: int, user_id: int):
    """به‌جای حذف کامل، فقط علامت می‌زنیم که رفته - تا اگه برگشت، بشناسیمش."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE group_members SET is_present=0, updated_at=? WHERE chat_id=? AND user_id=?",
        (datetime.utcnow().isoformat(), chat_id, user_id),
    )
    conn.commit()
    conn.close()


def remove_group_member(chat_id: int, user_id: int):
    """حذف کامل از دیتابیس (برای پاک‌سازی دستی؛ رفتار عادی رفتن از گروه از mark_member_left استفاده می‌کنه)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM group_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


# ---- حافظه کولیبا: یادداشت‌های آزاد درباره‌ی اعضا (خودشون یا اعضای دیگه توسط ادمین) -------
def add_memory_note(chat_id: int, user_id: int, user_name: str, note_text: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO koliba_memory (chat_id, user_id, user_name, note_text, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, user_id, user_name, note_text.strip(), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_notes_for_user(chat_id: int, user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT note_text FROM koliba_memory WHERE chat_id=? AND user_id=? ORDER BY id DESC",
        (chat_id, user_id),
    )
    notes = [r["note_text"] for r in c.fetchall()]
    conn.close()
    return notes


def get_random_note(chat_id: int):
    """یه یادداشت رندوم از این گروه برمی‌گردونه (برای پیگیریِ گاه‌به‌گاه تو یادآوری غیبت اعضا)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, user_name, note_text FROM koliba_memory WHERE chat_id=? ORDER BY RANDOM() LIMIT 1",
        (chat_id,),
    )
    row = c.fetchone()
    if row:
        c.execute("UPDATE koliba_memory SET mentioned_count = mentioned_count + 1 WHERE id=?", (row["id"],))
        conn.commit()
    conn.close()
    return row


# ---- پک‌های دیالوگ (لحن‌ها) - از فایل‌هایی روی گیت‌هاب خونده می‌شن، نه هاردکد تو کد ربات -----
async def _fetch_url_text(url: str) -> str:
    def _blocking():
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode("utf-8")
    return await asyncio.to_thread(_blocking)


async def get_pack(pack_name: str, force_refresh: bool = False) -> dict:
    """
    یه پک (لحن) رو از آدرس گیت‌هابش می‌خونه و کش می‌کنه. فایل باید یه فایل پایتونی باشه که
    یه دیکشنری (رشته -> لیست رشته) توش تعریف شده - اسم متغیر مهم نیست. با ast.literal_eval
    خونده می‌شه، یعنی هیچ کدی از اون فایل اجرا نمی‌شه (امن در برابر کد مخرب).
    """
    info = PACK_DEFINITIONS.get(pack_name)
    if not info:
        return {}
    url = pack_url(info)
    if not url:
        return {}
    cache = _pack_cache.setdefault(pack_name, {"data": {}, "loaded_at": None})
    now = datetime.utcnow()
    if not force_refresh and cache["loaded_at"] is not None:
        if (now - cache["loaded_at"]).total_seconds() < DEFAULT_PACK_REFRESH_SECONDS:
            return cache["data"]
    try:
        text = await _fetch_url_text(url)
        tree = ast.parse(text, mode="exec")
        pack = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                try:
                    candidate = ast.literal_eval(node.value)
                except Exception:
                    continue
                if isinstance(candidate, dict):
                    pack = candidate
                    break
        if not isinstance(pack, dict):
            raise ValueError("هیچ دیکشنری معتبری تو فایل پیدا نشد.")
        cache["data"] = pack
        cache["loaded_at"] = now
    except Exception as e:
        logger.warning(f"خطا در گرفتن پک «{pack_name}» از گیت‌هاب: {e}")
        # اگه قبلاً یه نسخه‌ی موفق کش شده باشه، همون برمی‌گرده؛ وگرنه دیکشنری خالی
    return cache["data"]


def find_best_match_across_packs(packs_data: list, text: str):
    """
    چندتا پک هم‌زمان فعال می‌تونن باشن؛ این تابع تو همه‌شون دنبال طولانی‌ترین (دقیق‌ترینِ) کلمه‌ای
    می‌گرده که تو متن پیام باشه. اگه چندتا پک هم‌زمان همون کلمه رو داشته باشن، یکی‌شون رندوم انتخاب می‌شه.
    """
    text_norm = text.strip()
    if not text_norm:
        return None
    best_len = -1
    candidates = []
    for pack in packs_data:
        if not pack:
            continue
        for trig, answers in pack.items():
            trig_s = trig.strip() if isinstance(trig, str) else ""
            if trig_s and trig_s in text_norm:
                if len(trig_s) > best_len:
                    best_len = len(trig_s)
                    candidates = [answers]
                elif len(trig_s) == best_len:
                    candidates.append(answers)
    if not candidates:
        return None
    chosen = random.choice(candidates)
    if chosen:
        return random.choice(chosen)
    return None


# ---- اعضای فعال (تنظیمات پک‌های فعال هر گروه) --------------------------------
def get_active_packs(chat_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT pack_name FROM active_packs WHERE chat_id=?", (chat_id,))
    rows = [r["pack_name"] for r in c.fetchall()]
    conn.close()
    return rows


def toggle_active_pack(chat_id: int, pack_name: str) -> bool:
    """پک رو روشن/خاموش می‌کنه و برمی‌گردونه که الان روشنه (True) یا خاموش (False)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM active_packs WHERE chat_id=? AND pack_name=?", (chat_id, pack_name))
    exists = c.fetchone() is not None
    if exists:
        c.execute("DELETE FROM active_packs WHERE chat_id=? AND pack_name=?", (chat_id, pack_name))
    else:
        c.execute("INSERT INTO active_packs (chat_id, pack_name) VALUES (?, ?)", (chat_id, pack_name))
    conn.commit()
    conn.close()
    return not exists


# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------
def admin_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔒 قفل کردن گروه", callback_data="adm_lock"),
             InlineKeyboardButton("🔓 باز کردن گروه", callback_data="adm_unlock")],
            [InlineKeyboardButton("🏷 دادن لقب به عضو", callback_data="adm_title")],
            [InlineKeyboardButton("⚠️ ثبت اخطار", callback_data="adm_warn_add"),
             InlineKeyboardButton("✅ حذف اخطار", callback_data="adm_warn_remove")],
            [InlineKeyboardButton("🚫 کلمات ممنوعه", callback_data="adm_forbidden")],
            [InlineKeyboardButton("🗑 حذف پیام‌ها", callback_data="adm_delete_msgs")],
            [InlineKeyboardButton("🌟 افراد ویژه", callback_data="adm_special")],
            [InlineKeyboardButton("🔇 سکوت‌شده‌ها", callback_data="adm_mutelist")],
            [InlineKeyboardButton("🚫 لیست مسدودی", callback_data="adm_banlist")],
            [InlineKeyboardButton("⚙️ تنظیمات گروه", callback_data="adm_settings")],
            [InlineKeyboardButton("🗨 کلمه‌های آماده", callback_data="adm_ready_words")],
            [InlineKeyboardButton("👥 اعضا", callback_data="adm_member_count"),
             InlineKeyboardButton("🔗 لینک دعوت", callback_data="adm_invite_link")],
            [InlineKeyboardButton("❌ بستن پنل", callback_data="adm_close_panel")],
        ]
    )


def back_to_admin_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به پنل ادمین", callback_data="adm_back")]])


def main_menu_keyboard_for(bot_username: str):
    """کیبورد شیشه‌ای کامل - همچنان داخل گروه استفاده می‌شه، بدون تغییر."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزودن به گروه", url=f"https://t.me/{bot_username}?startgroup=true")],
            [InlineKeyboardButton("📚 یاد دادن کلمه", callback_data="learn_word")],
            [InlineKeyboardButton("📋 دیدن کلمات ساخته شده", callback_data="view_words")],
        ]
    )


GAME_PANEL_URL = os.environ.get("GAME_PANEL_URL", "").strip()  # آدرسِ HTTPS پنلِ بازی‌ها؛ خالی = هنوز دیپلوی نشده


def private_menu_keyboard(bot_username: str):
    """تو پیوی دکمه‌ی افزودن به گروه + دکمه‌ی توضیحات نگه داشته می‌شه؛ بقیه از منوی ☰ خود ربات در دسترسه.
    دکمه‌ی «پنلِ بازی‌ها» فقط وقتی نشون داده می‌شه که GAME_PANEL_URL تنظیم شده باشه (یعنی پنل
    واقعاً یه‌جا با HTTPS دیپلوی شده)؛ وگرنه تلگرام از دکمه‌ی web_app با URL خالی خطا می‌ده."""
    buttons = [
        [InlineKeyboardButton("➕ افزودن به گروه", url=f"https://t.me/{bot_username}?startgroup=true")],
    ]
    if GAME_PANEL_URL:
        buttons.append([InlineKeyboardButton("🎮 پنلِ بازی‌ها", web_app=WebAppInfo(url=GAME_PANEL_URL))])
    buttons.append([InlineKeyboardButton("ℹ️ توضیحات", callback_data="start_help")])
    return InlineKeyboardMarkup(buttons)


HELP_TEXT = (
    "📖 یه دور سریع با قابلیت‌های ربات چتر:\n\n"
    "🛡 مدیریت گروه: رو پیامِ یه عضو ریپلای بزن و بنویس «بن»، «اخطار»، «سکوت» یا «خفه» "
    "(با یه عدد = موقت، مثلاً «سکوت ۱۰»)، «آزاد» برای درآوردن از سکوت، «لقب <متن>» برای دادنِ لقب. "
    "«پین»/«آنپین» هم برای سنجاق‌کردنِ پیام.\n\n"
    "🎮 بازی: تو گروه بنویس «بازی» تا بین دوز (کلاسیک/سه‌مهره‌ای)، سنگ‌کاغذقیچی، و شطرنج انتخاب کنی؛ "
    "با ربات یا با یه عضوِ دیگه.\n\n"
    "🧠 حافظه: «یادت باشه <متن>» تا یه چیزی رو درباره‌ی خودت یادم بمونه، «چی یادته» برای دیدنشون.\n\n"
    "🏷 تگ همگانی: با ریپلای‌زدن رو یه پیام و نوشتنِ «تگ همگانی» (فقط مالکِ گروه).\n\n"
    "💬 پیام از طرفِ ربات: تو گروه بنویس «بگو <متن>»، یا رو یه عکس/گیف ریپلای بزن و بنویس «بگو». "
    "از همینجا (پیویِ من) هم می‌تونی با نوشتنِ «چت» به یه گروهی که توش ادمینی وصل بشی و کاملاً بی‌سروصدا پیام بفرستی.\n\n"
    "⚙️ پنل مدیریت: تو گروه بنویس «پنل» یا بزن /admin.\n\n"
    "هر سوالی داشتی همینجا بپرس 🌸"
)


async def start_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="start_back")]])
    await query.edit_message_text(HELP_TEXT, reply_markup=keyboard)


async def start_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_username = (await context.bot.get_me()).username
    text = (
        "هی سلام! 👋 من ربات چتر هستم.\n"
        "می‌تونی منو به گروهت اضافه کنی تا یه خاطره‌ی خوش با هم داشته باشیم 🌸\n\n"
        "برای یاد دادن کلمه یا دیدن کلمات ساخته‌شده، از دکمه‌ی منو (☰) کنار پیام استفاده کن.\n"
        "اگه می‌خوای با ربات بیشتر آشنا بشی، گزینه‌ی «توضیحات» رو بزن 👇"
    )
    await query.edit_message_text(text, reply_markup=private_menu_keyboard(bot_username))


# ---------------------------------------------------------------------------
# کمکی: پیدا کردن گروهی که باید کلمات روش ثبت/چک بشه
# ---------------------------------------------------------------------------
async def resolve_manage_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    اگه داخل گروه هستیم، همون گروه هدفه (رفتار قبلی، بدون تغییر).
    اگه تو پیویم، باید بفهمیم منظور کدوم گروهه:
      - اگه قبلا برای همین کاربر مشخص شده، همونو برمی‌گردونه.
      - اگه کاربر فقط تو یه گروه شناخته شده (پیام داده/ادمینه)، همونو خودکار انتخاب می‌کنه.
      - اگه صفر یا بیشتر از یکی بود، None برمی‌گردونه و extra اطلاعات لازم برای تصمیم‌گیری رو می‌ده.
    """
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        context.user_data["manage_chat_id"] = chat.id
        return chat.id, None

    if "manage_chat_id" in context.user_data:
        return context.user_data["manage_chat_id"], None

    groups = get_user_groups(update.effective_user.id)
    if not groups:
        return None, "no_groups"
    if len(groups) == 1:
        context.user_data["manage_chat_id"] = groups[0]["chat_id"]
        return groups[0]["chat_id"], None
    return None, groups


def active_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """چت هدفی که باید کلمه توش ثبت/ویرایش بشه - برای گروه همون گروه، برای پیوی همونی که قبلا resolve شده."""
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        return chat.id
    return context.user_data.get("manage_chat_id", chat.id)


async def _reply(update: Update, text: str, reply_markup=None):
    """به روزرسانی/ارسال پیام، چه از طریق دکمه شیشه‌ای و چه از طریق دستور متنی."""
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def is_member_of_required_channel(bot, user_id: int) -> bool:
    """اگه REQUIRED_CHANNEL_ID تنظیم نشده باشه، محدودیتی اعمال نمی‌شه (True برمی‌گردونه)."""
    if not REQUIRED_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def channel_gate_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 عضویت در کانال", url=REQUIRED_CHANNEL_URL)],
            [InlineKeyboardButton("✅ عضو شدم", callback_data="check_channel_join")],
        ]
    )


CHANNEL_GATE_TEXT = (
    "هی سلام! 👋 من ربات چتر هستم.\n\n"
    "اگه می‌خوای از قابلیت‌هام استفاده کنی و یادشون بگیری، اول تو کانالم عضو شو 👇\n"
    "بعد از عضویت، بزن رو «✅ عضو شدم»."
)


async def check_channel_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    is_member = await is_member_of_required_channel(context.bot, update.effective_user.id)
    if not is_member:
        await query.answer("😅 هنوز تو کانال عضو نشدی! اول عضو شو، بعد بزن رو این دکمه.", show_alert=True)
        return
    await query.answer("✅ خوش اومدی!")
    bot_username = (await context.bot.get_me()).username
    text = (
        "هی سلام! 👋 من ربات چتر هستم.\n"
        "می‌تونی منو به گروهت اضافه کنی تا یه خاطره‌ی خوش با هم داشته باشیم 🌸\n\n"
        "برای یاد دادن کلمه یا دیدن کلمات ساخته‌شده، از دکمه‌ی منو (☰) کنار پیام استفاده کن.\n"
        "اگه می‌خوای با ربات بیشتر آشنا بشی، گزینه‌ی «توضیحات» رو بزن 👇"
    )
    await query.edit_message_text(text, reply_markup=private_menu_keyboard(bot_username))


# ---------------------------------------------------------------------------
# نسخه‌ی ربات
# ---------------------------------------------------------------------------
async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 نسخه‌ی فعلیِ ربات: {BOT_VERSION}")


# ---------------------------------------------------------------------------
# دستور /start و منوی اصلی
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    bot_username = (await context.bot.get_me()).username
    if chat.type in ("group", "supergroup"):
        record_user_group(update.effective_user.id, chat.id, chat.title)
        keyboard = main_menu_keyboard_for(bot_username)
        text = (
            "هی سلام! 👋 من ربات چتر هستم.\n"
            "از منوی زیر می‌تونی یکی از قابلیت‌ها رو انتخاب کنی:"
        )
        await update.message.reply_text(text, reply_markup=keyboard)
        return

    # پیوی: اگه کانالِ اجباری تنظیم شده باشه، قبل از منوی اصلی عضویت رو چک می‌کنیم
    if not await is_member_of_required_channel(context.bot, update.effective_user.id):
        await update.message.reply_text(CHANNEL_GATE_TEXT, reply_markup=channel_gate_keyboard())
        return

    keyboard = private_menu_keyboard(bot_username)
    text = (
        "هی سلام! 👋 من ربات چتر هستم.\n"
        "می‌تونی منو به گروهت اضافه کنی تا یه خاطره‌ی خوش با هم داشته باشیم 🌸\n\n"
        "برای یاد دادن کلمه یا دیدن کلمات ساخته‌شده، از دکمه‌ی منو (☰) کنار پیام استفاده کن.\n"
        "اگه می‌خوای با ربات بیشتر آشنا بشی، گزینه‌ی «توضیحات» رو بزن 👇"
    )
    await update.message.reply_text(text, reply_markup=keyboard)


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_username = (await context.bot.get_me()).username
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        await query.edit_message_text(
            "منوی اصلی 🌸\nاز گزینه‌های زیر یکی رو انتخاب کن:",
            reply_markup=main_menu_keyboard_for(bot_username),
        )
    else:
        await query.edit_message_text(
            "منوی اصلی 🌸\nبرای یاد دادن کلمه یا دیدن کلمات، از دکمه‌ی منو (☰) استفاده کن.",
            reply_markup=private_menu_keyboard(bot_username),
        )


# ---------------------------------------------------------------------------
# قابلیت: یاد دادن کلمه (مکالمه ۲ مرحله‌ای) - هم با دکمه شیشه‌ای، هم با دستور /addword
# هر ادمین لیست خصوصیِ خودش رو می‌سازه؛ تو گروه فقط لیست خودِ مالک واقعی گروه اجرا می‌شه.
# ---------------------------------------------------------------------------
async def learn_word_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    chat_id, extra = await resolve_manage_chat(update, context)
    if chat_id is None:
        if extra == "no_groups":
            await _reply(
                update,
                "اول باید ربات رو به یه گروه اضافه کنی و اونجا یه پیام (مثلا /start) بفرستی، "
                "بعد از همینجا می‌تونی براش کلمه یاد بدی.",
            )
        else:
            buttons = [
                [InlineKeyboardButton(g["title"] or str(g["chat_id"]), callback_data=f"selgrp_{g['chat_id']}_learn")]
                for g in extra
            ]
            await _reply(update, "برای کدوم گروه می‌خوای کلمه یاد بدی؟", InlineKeyboardMarkup(buttons))
        return ConversationHandler.END

    if not await is_user_admin(update, context, update.effective_user.id, chat_id):
        await _reply(update, "⛔ داداش این کارو فقط ادمینا می‌تونن بکنن.")
        return ConversationHandler.END

    await _reply(
        update,
        "اون کلمه‌ای که می‌خوای وقتی مردم گفتن ربات جواب بده رو بفرست.\nمثلا: سلام\n\n"
        "توجه: این لیستِ خصوصیِ خودته. تو گروه فقط لیستِ مالکِ واقعیِ گروه اجرا می‌شه.",
    )
    return LEARN_WORD_WAIT_TRIGGER


async def learn_word_got_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_trigger"] = update.message.text
    await update.message.reply_text(
        f"باشه، حالا اون جوابی که می‌خوای من به «{update.message.text}» بدم رو بفرست."
    )
    return LEARN_WORD_WAIT_ANSWER


async def learn_word_got_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trigger = context.user_data.pop("new_trigger", None)
    if not trigger:
        await update.message.reply_text("یه مشکلی پیش اومد، دوباره از منو شروع کن.")
        return ConversationHandler.END
    chat_id = active_chat_id(update, context)
    teacher_id = update.effective_user.id
    code = add_learned_word(chat_id, teacher_id, trigger, update.message.text)
    await update.message.reply_text(
        f"✅ ثبت شد (تو لیست خصوصی خودت)!\nکد: {code}\nکلمه: {trigger}\nجواب: {update.message.text}"
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# قابلیت: دیدن کلمات ساخته شده - هم با دکمه شیشه‌ای، هم با دستور /mywords
# هر کسی فقط لیست خودش رو می‌بینه؛ لیست بقیه (حتی لیست مالک) براش قابل دیدن نیست.
# ---------------------------------------------------------------------------
def build_words_list_text(chat_id: int, teacher_id: int) -> str:
    rows = list_learned_words(chat_id, teacher_id)
    if not rows:
        return "هنوز هیچ کلمه‌ای تو لیست خصوصی تو یاد داده نشده."
    lines = ["لیست کلمات یاد گرفته شده‌ی تو:\n"]
    for r in rows:
        answers = json.loads(r["answers"])
        answer_display = answers[0] if len(answers) == 1 else " | ".join(answers)
        lines.append(f"کد: {r['code']}\nکلمه: {r['trigger_word']}\nجواب: {answer_display}\n")
    return "\n".join(lines)


def words_list_keyboard(chat_id: int, teacher_id: int):
    rows = list_learned_words(chat_id, teacher_id)
    buttons = []
    row_buf = []
    for r in rows:
        row_buf.append(InlineKeyboardButton(f"✏️ کد {r['code']}", callback_data=f"word_edit_{r['code']}"))
        if len(row_buf) == 3:
            buttons.append(row_buf)
            row_buf = []
    if row_buf:
        buttons.append(row_buf)
    buttons.append([InlineKeyboardButton("➕ اضافه کردن جدید", callback_data="learn_word")])
    if rows:
        buttons.append([InlineKeyboardButton("🗑 حذف یه کلمه", callback_data="worddel_start")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


async def view_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    chat_id, extra = await resolve_manage_chat(update, context)
    if chat_id is None:
        if extra == "no_groups":
            await _reply(
                update,
                "اول باید ربات رو به یه گروه اضافه کنی و اونجا یه پیام (مثلا /start) بفرستی، "
                "بعد از همینجا می‌تونی کلمات خودت رو ببینی.",
            )
        else:
            buttons = [
                [InlineKeyboardButton(g["title"] or str(g["chat_id"]), callback_data=f"selgrp_{g['chat_id']}_view")]
                for g in extra
            ]
            await _reply(update, "کلمات کدوم گروه رو می‌خوای ببینی؟", InlineKeyboardMarkup(buttons))
        return
    teacher_id = update.effective_user.id
    await _reply(update, build_words_list_text(chat_id, teacher_id), words_list_keyboard(chat_id, teacher_id))


async def select_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کاربر تو پیوی گروه هدف رو از لیست انتخاب کرده. callback_data شکل selgrp_<chat_id>_<action> است."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    chat_id = int(parts[1])
    action = parts[2]
    context.user_data["manage_chat_id"] = chat_id
    if action == "learn":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ ادامه: یاد دادن کلمه", callback_data="learn_word")]])
        await query.edit_message_text("گروه انتخاب شد. برای ادامه دکمه زیر رو بزن:", reply_markup=keyboard)
    else:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ ادامه: دیدن کلمات", callback_data="view_words")]])
        await query.edit_message_text("گروه انتخاب شد. برای ادامه دکمه زیر رو بزن:", reply_markup=keyboard)


async def word_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = int(query.data.split("_")[-1])
    context.user_data["edit_code"] = code
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 تغییر جواب", callback_data="wact_change")],
            [InlineKeyboardButton("➕ اضافه کردن جواب دیگر (رندوم)", callback_data="wact_add_extra")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="view_words")],
        ]
    )
    await query.edit_message_text(f"حالتت رو انتخاب کن (کد {code}):", reply_markup=keyboard)


async def word_action_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = active_chat_id(update, context)
    if not await is_user_admin(update, context, update.effective_user.id, chat_id):
        await query.answer("⛔ دستِ ادمینا رو کلمه‌هاست فقط داداش.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["edit_mode"] = "change"
    await query.edit_message_text("جواب جدید رو بفرست تا جایگزین جواب قبلی بشه.")
    return EDIT_WORD_WAIT_ANSWER


async def word_action_add_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = active_chat_id(update, context)
    if not await is_user_admin(update, context, update.effective_user.id, chat_id):
        await query.answer("⛔ دستِ ادمینا رو کلمه‌هاست فقط داداش.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["edit_mode"] = "extra"
    await query.edit_message_text(
        "جواب اضافه رو بفرست. از این به بعد ربات به صورت رندوم یکی از جواب‌ها رو انتخاب می‌کنه."
    )
    return EDIT_WORD_WAIT_ANSWER


async def word_edit_got_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = context.user_data.pop("edit_code", None)
    mode = context.user_data.pop("edit_mode", None)
    chat_id = active_chat_id(update, context)
    teacher_id = update.effective_user.id
    if code is None:
        await update.message.reply_text("یه مشکلی پیش اومد، دوباره امتحان کن.")
        return ConversationHandler.END
    if mode == "change":
        set_word_answer(chat_id, teacher_id, code, update.message.text)
        await update.message.reply_text(f"✅ جواب کد {code} عوض شد.")
    else:
        add_extra_answer(chat_id, teacher_id, code, update.message.text)
        await update.message.reply_text(f"✅ جواب جدید به کد {code} اضافه شد.")
    return ConversationHandler.END


# ---- حذف کامل یه کلمه با کد -------------------------------------------------
async def worddel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = active_chat_id(update, context)
    if not await is_user_admin(update, context, update.effective_user.id, chat_id):
        await query.answer("⛔ حذفِ کلمه کارِ ادمیناست داداش.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("کد کلمه‌ای که می‌خوای کامل حذف بشه رو بفرست.")
    return DELETE_WORD_WAIT_CODE


async def worddel_got_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        code = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("لطفا فقط عدد کدِ کلمه رو بفرست.")
        return DELETE_WORD_WAIT_CODE
    chat_id = active_chat_id(update, context)
    teacher_id = update.effective_user.id
    if delete_learned_word(chat_id, teacher_id, code):
        await update.message.reply_text(f"✅ کلمه با کد {code} کامل حذف شد.")
    else:
        await update.message.reply_text(f"کلمه‌ای با کد {code} تو لیست خودت پیدا نکردم.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# پنل ادمین: ورود
# ---------------------------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("این دستور فقط داخل گروه کار می‌کنه.")
        return
    if not await is_user_admin(update, context, user.id, chat.id):
        await update.message.reply_text("⛔ اینجا فقط ادمینا راه دارن داداش.")
        return
    record_user_group(user.id, chat.id, chat.title)
    await get_group_owner_id(context, chat.id)  # کش مالک گروه رو تازه/آماده نگه می‌داره
    sent = await update.message.reply_text("پنل مدیریت گروه 🛠", reply_markup=admin_menu_keyboard())
    # یادمون می‌مونه این پنل با کدوم پیام باز شد، تا وقتی بسته شد اونم پاک بشه
    context.chat_data[f"panel_trigger:{sent.message_id}"] = update.message.message_id


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("پنل مدیریت گروه 🛠", reply_markup=admin_menu_keyboard())


async def adm_close_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    panel_msg_id = update.callback_query.message.message_id
    trigger_msg_id = context.chat_data.pop(f"panel_trigger:{panel_msg_id}", None)
    try:
        await update.callback_query.message.delete()
    except Exception:
        await update.callback_query.edit_message_text("پنل بسته شد. ✅")
    if trigger_msg_id:
        try:
            await context.bot.delete_message(chat_id, trigger_msg_id)
        except Exception:
            pass


async def _guard_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """چک می‌کنه که آیا کاربری که روی دکمه ادمین کلیک کرده واقعا ادمینه یا نه."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_user_admin(update, context, user_id, chat_id):
        await query.answer("⛔ این دکمه مالِ تو نیست داداش.", show_alert=True)
        return False
    await query.answer()
    return True


# ---- قفل / باز کردن گروه ----------------------------------------------------
async def adm_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.set_chat_permissions(
            chat_id, ChatPermissions(can_send_messages=False)
        )
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO group_settings (chat_id, locked) VALUES (?, 1) "
            "ON CONFLICT(chat_id) DO UPDATE SET locked=1",
            (chat_id,),
        )
        conn.commit()
        conn.close()
        await update.callback_query.edit_message_text("🔒 گروه قفل شد.", reply_markup=back_to_admin_keyboard())
    except Exception as e:
        await update.callback_query.edit_message_text(
            f"خطا در قفل کردن گروه (باید ربات ادمین با دسترسی مدیریت گروه باشه): {e}",
            reply_markup=back_to_admin_keyboard(),
        )


async def adm_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO group_settings (chat_id, locked) VALUES (?, 0) "
            "ON CONFLICT(chat_id) DO UPDATE SET locked=0",
            (chat_id,),
        )
        conn.commit()
        conn.close()
        await update.callback_query.edit_message_text("🔓 گروه باز شد.", reply_markup=back_to_admin_keyboard())
    except Exception as e:
        await update.callback_query.edit_message_text(
            f"خطا در باز کردن گروه: {e}", reply_markup=back_to_admin_keyboard()
        )


# ---- دادن لقب به عضو --------------------------------------------------------
async def adm_title_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text(
        "روی پیام همون عضو ریپلای کن و لقب مورد نظر رو به عنوان جواب بفرست.\n"
        "(باید روی یک پیام از عضو مورد نظر در گروه ریپلای کنی)"
    )
    return ADD_TITLE_WAIT_TEXT


async def adm_title_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("باید روی پیام همون عضو ریپلای کنی. دوباره امتحان کن.")
        return ADD_TITLE_WAIT_TEXT
    target = update.message.reply_to_message.from_user
    if target is None:
        await update.message.reply_text("نتونستم فرستنده‌ی این پیام رو شناسایی کنم.")
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    title_text = update.message.text.strip()[:16]
    set_nickname(chat_id, target.id, title_text, update.effective_user.id)
    await update.message.reply_text(f"✅ لقب «{title_text}» به {target.first_name} داده شد.")
    return ConversationHandler.END


# ---- ثبت / حذف اخطار --------------------------------------------------------
async def perform_warn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target, reason: str, reply_target_message=None):
    """منطق مشترک ثبت اخطار: هم پنل ادمین (مکالمه‌ای) و هم دستور سریع «اخطار» از این استفاده می‌کنن."""
    if await is_owner_protected(context, chat_id, target.id):
        text = OWNER_PROTECTED_TEXT.format(name=target.first_name)
        if reply_target_message is not None:
            await reply_tracked(reply_target_message, text)
        else:
            await send_tracked(context.bot, chat_id, text)
        return
    count = add_warning(chat_id, target.id, reason)
    log_mod_action(chat_id, 0, "ادمین", "warn", target.id, target.first_name or "", reason)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👁 مشاهده پیام", callback_data=f"warnview_{target.id}")],
            [InlineKeyboardButton("✅ حذف اخطار", callback_data=f"warnremove_{target.id}")],
        ]
    )
    text = f"⚠️ {display_name(chat_id, target)} جان، این شد اخطارِ {count} از {MAX_WARNINGS} تا. یه‌کم آروم‌تر داداش.\nدلیل: {reason}"
    if reply_target_message is not None:
        await reply_tracked(reply_target_message, text, reply_markup=keyboard)
    else:
        await send_tracked(context.bot, chat_id, text, reply_markup=keyboard)
    if count >= MAX_WARNINGS:
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            record_ban(chat_id, target.id, target.first_name or "کاربر", f"{MAX_WARNINGS} اخطار")
            await send_tracked(
                context.bot, chat_id, f"🚫 {target.first_name} به دلیل رسیدن به {MAX_WARNINGS} اخطار از گروه حذف شد."
            )
            remove_all_warnings(chat_id, target.id)
        except Exception as e:
            await send_tracked(context.bot, chat_id, f"نتونستم کاربر رو حذف کنم: {e}")


# ---- اخطار خودکار مشترک برای هر چیزی که تو گروه ممنوع اعلام شده (کلمه ممنوعه/رسانه/فوروارد/لینک) ----
async def warn_and_maybe_ban(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, violation_text: str):
    if await is_owner_protected(context, chat_id, user.id):
        return
    count = add_warning(chat_id, user.id, violation_text)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👁 مشاهده پیام", callback_data=f"warnview_{user.id}")],
            [InlineKeyboardButton("✅ حذف اخطار", callback_data=f"warnremove_{user.id}")],
        ]
    )
    await send_tracked(
        context.bot,
        chat_id,
        f"داداش {user.first_name} جان، اینجا {violation_text} ممنوعه‌ها، بی‌خیالش شو! ({count} اخطار از {MAX_WARNINGS})",
        reply_markup=keyboard,
    )
    if count >= MAX_WARNINGS:
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            record_ban(chat_id, user.id, user.first_name or "کاربر", f"{MAX_WARNINGS} اخطار")
            await send_tracked(
                context.bot, chat_id, f"🚫 {user.first_name} اخطارش پر شد ({MAX_WARNINGS} تا)، رفت بیرون."
            )
            remove_all_warnings(chat_id, user.id)
        except Exception:
            pass


def _is_admin_command_word(text: str) -> bool:
    """تشخیص می‌ده که آیا این متن یکی از دستورهای سریعِ ادمینیه (برای موردی که هدفش خودِ رباته)."""
    if text in ("بن", "صیکتیر", "اخطار", "آزاد", "سکوت", "خفه", "حذف لقب", "کولیبا ویژه"):
        return True
    if text.startswith("اخطار ") or text.startswith("لقب "):
        return True
    if re.match(r"^(سکوت|خفه)\s+\d+$", text):
        return True
    return False


BOT_SELF_TARGET_REPLIES = [
    "😏 نچ، رو من از این کارا جواب نمی‌ده.",
    "🤖 من که ادمینِ خودمم، این دستورا رو من می‌زنم نه رو من!",
    "😂 خیلی هم دلت بخواد سکوتم کنی.",
    "🫡 چشم قربان! …نه بابا شوخی کردم، رو خودم که کار نمی‌کنه.",
    "🙃 امتحانِ جالبی بود، ولی جواب نداد.",
]


async def _safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ماشین‌حسابِ ساده - یه محاسبه‌گرِ امن (بدونِ eval خام؛ فقط عملگرهای ریاضیِ معمولی).
# ---------------------------------------------------------------------------
_CALC_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CALC_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_CALC_ALLOWED_CHARS_RE = re.compile(r"^[0-9\.\+\-\*/x×÷\^%()\s]+$")
_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"


def _calc_eval_node(node):
    if isinstance(node, ast.Expression):
        return _calc_eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BINOPS:
        left = _calc_eval_node(node.left)
        right = _calc_eval_node(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 10**8):
            raise ValueError("عدد خیلی بزرگه")
        return _CALC_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARY:
        return _CALC_UNARY[type(node.op)](_calc_eval_node(node.operand))
    raise ValueError("این بخش از عبارت رو نمی‌فهمم")


def calculate_expression(expr: str):
    """محاسبه‌ی امنِ یه عبارتِ ریاضیِ ساده. برمی‌گردونه (True, نتیجه) یا (False, None)."""
    if not expr:
        return False, None
    cleaned = expr.strip()
    cleaned = cleaned.replace("×", "*").replace("x", "*").replace("X", "*").replace("÷", "/").replace("^", "**")
    cleaned = cleaned.replace("،", "").replace(",", "")
    for i, d in enumerate(_PERSIAN_DIGITS):
        cleaned = cleaned.replace(d, str(i))
    for i, d in enumerate(_ARABIC_DIGITS):
        cleaned = cleaned.replace(d, str(i))
    check = cleaned.replace("**", "^")  # فقط برای چک‌کردنِ کاراکترهای مجاز، ** رو موقتاً به ^ برمی‌گردونیم
    if not check or not _CALC_ALLOWED_CHARS_RE.match(check.replace("x", "*").replace("×", "*")):
        return False, None
    try:
        tree = ast.parse(cleaned, mode="eval")
        result = _calc_eval_node(tree)
    except ZeroDivisionError:
        return False, None
    except Exception:
        return False, None
    if isinstance(result, float) and (result != result or result in (float("inf"), float("-inf"))):
        return False, None  # nan / inf
    return True, result


def format_calc_result(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, float):
        value = round(value, 6)
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        return s
    return str(value)


# ---------------------------------------------------------------------------
# «چت» از طریق پیوی - وصل کردنِ پیویِ یه ادمین به یه گروه، تا بتونه بدون این‌که هیچ
# پیامی از خودش تو گروه بره، از طریق پیوی به اسمِ ربات پیام بفرسته.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# «چت» از طریق پیوی - وصل کردنِ پیویِ یه ادمین به یه گروه، تا بتونه بدون این‌که هیچ
# پیامی از خودش تو گروه بره، از طریق پیوی به اسمِ ربات پیام (متن یا عکس/گیف/ویدیو) بفرسته.
# ---------------------------------------------------------------------------
TAG_SUFFIX_RE = re.compile(r"^(?P<body>.*?)\s+تگ\s+(?P<target>همگانی|@?[A-Za-z0-9_]{3,})\s*$", re.DOTALL)
TAG_WHOLE_RE = re.compile(r"^تگ\s+(?P<target>همگانی|@?[A-Za-z0-9_]{3,})$")


def parse_tag_directive(text: str):
    """آخرِ متن رو برای «تگ همگانی» یا «تگ @یوزرنیم» چک می‌کنه (چه با متنی قبلش، چه تنها).
    برمی‌گردونه: (باقی‌ماندهٔ متن، حالت [None|'all'|'user'], یوزرنیم یا None)"""
    if not text:
        return "", None, None
    text = text.strip()
    m_whole = TAG_WHOLE_RE.match(text)
    if m_whole:
        target = m_whole.group("target")
        return ("", "all", None) if target == "همگانی" else ("", "user", target.lstrip("@"))
    m = TAG_SUFFIX_RE.match(text)
    if not m:
        return text, None, None
    body = m.group("body").strip()
    target = m.group("target")
    if target == "همگانی":
        return body, "all", None
    return body, "user", target.lstrip("@")


def build_invisible_tag_html(chat_id: int, mode: str, username: str = None):
    """لینک‌های نامرئیِ تگ رو می‌سازه - تلگرام واقعاً به همون نفر/نفرا پینگ می‌فرسته،
    ولی هیچ اسم/یوزرنیمی تو متنِ پیام دیده نمی‌شه. برای «user» اگه کسی پیدا نشه None برمی‌گردونه."""
    if mode == "all":
        members = get_group_members(chat_id)
        return "".join(f'<a href="tg://user?id={m["user_id"]}">&#8203;</a>' for m in members)
    if mode == "user" and username:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT user_id FROM group_members WHERE chat_id=? AND LOWER(username)=LOWER(?) ORDER BY updated_at DESC LIMIT 1",
            (chat_id, username),
        )
        row = c.fetchone()
        conn.close()
        return f'<a href="tg://user?id={row["user_id"]}">&#8203;</a>' if row else None
    return ""


async def _pv_forward_text(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, link, raw_text: str):
    body, tag_mode, tag_username = parse_tag_directive(raw_text)
    tag_html = None
    if tag_mode:
        tag_html = build_invisible_tag_html(chat_id, tag_mode, tag_username)
        if tag_mode == "user" and tag_html is None:
            await update.message.reply_text(
                f"کاربر @{tag_username} رو تو «{link['chat_title']}» نشناختم (باید حداقل یه پیام اونجا داده باشه)."
            )
            return
    if not body and not tag_html:
        await update.message.reply_text("بعد از «چت» متنی که می‌خوای فرستاده بشه رو هم بنویس.")
        return
    try:
        if tag_html:
            final_text = html.escape(body) + tag_html if body else tag_html
            sent = await context.bot.send_message(chat_id, final_text, parse_mode=ParseMode.HTML)
        else:
            sent = await context.bot.send_message(chat_id, body)
        track_message(chat_id, sent.message_id)
        await update.message.reply_text(f"✅ فرستاده شد تو «{link['chat_title']}».")
    except Exception as e:
        await update.message.reply_text(f"نتونستم بفرستم: {e}")


async def _pv_forward_media(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, link, media_msg, raw_text: str):
    body, tag_mode, tag_username = parse_tag_directive(raw_text)
    tag_html = None
    if tag_mode:
        tag_html = build_invisible_tag_html(chat_id, tag_mode, tag_username)
        if tag_mode == "user" and tag_html is None:
            await update.message.reply_text(
                f"کاربر @{tag_username} رو تو «{link['chat_title']}» نشناختم (باید حداقل یه پیام اونجا داده باشه)."
            )
            return

    kwargs = {}
    if tag_html:
        kwargs["caption"] = (html.escape(body) + tag_html) if body else tag_html
        kwargs["parse_mode"] = ParseMode.HTML
    elif body:
        kwargs["caption"] = body

    try:
        if media_msg.photo:
            sent = await context.bot.send_photo(chat_id, media_msg.photo[-1].file_id, **kwargs)
        elif media_msg.animation:
            sent = await context.bot.send_animation(chat_id, media_msg.animation.file_id, **kwargs)
        elif media_msg.video:
            sent = await context.bot.send_video(chat_id, media_msg.video.file_id, **kwargs)
        else:
            await update.message.reply_text("این نوع فایل رو نمی‌تونم بفرستم؛ فقط عکس/گیف/ویدیو پشتیبانی می‌شه.")
            return
        track_message(chat_id, sent.message_id)
        await update.message.reply_text(f"✅ فرستاده شد تو «{link['chat_title']}».")
    except Exception as e:
        await update.message.reply_text(f"نتونستم بفرستم: {e}")


async def _pv_chat_show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = get_pv_link(user_id)
    if link:
        await update.message.reply_text(
            f"الان به «{link['chat_title']}» وصلی.\n"
            "برای فرستادنِ پیام تو اون گروه، بنویس:\n«چت متنی که می‌خوای»\n"
            "برای تگ‌کردنِ همه بی‌سروصدا: «چت متن تگ همگانی»\n"
            "برای تگ‌کردنِ یه نفر بی‌سروصدا: «چت متن تگ @یوزرنیم»\n"
            "برای فرستادنِ عکس: می‌تونی مستقیم با کپشنِ «چت» بفرستیش.\n"
            "برای گیف (چون تلگرام معمولاً اجازه‌ی کپشن‌گذاشتن روی گیف رو نمی‌ده): اول گیف رو بدونِ کپشن بفرست، بعد روش ریپلای بزن و بنویس «چت».\n\n"
            "برای عوض‌کردنِ گروه، یوزرنیمِ عمومیِ گروهِ جدید رو بفرست (مثلاً @nameofgroup)، یا بنویس «چت جدا شو»."
        )
        return
    groups = await find_admin_groups_for_user(update, context, user_id)
    if len(groups) == 1:
        chat_id, title = groups[0]
        set_pv_link(user_id, chat_id, title)
        await update.message.reply_text(
            f"✅ شناختمت! تو ادمینِ «{title}» هستی.\n"
            "خب شروع کن: از الان همین‌جا بنویس «چت <متن>» تا اون متن بی‌سروصدا تو همون گروه پست بشه."
        )
    elif len(groups) > 1:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(title, callback_data=f"pvlink_{chat_id}")] for chat_id, title in groups]
        )
        await update.message.reply_text("تو چند تا گروه ادمینی؛ کدومو وصل کنم؟", reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "هنوز نشناختمت به‌عنوان ادمینِ هیچ گروهی (باید حداقل یه پیام تو اون گروه فرستاده باشی تا بشناسمت).\n"
            "اگه گروهت عمومیه، یوزرنیمِ عمومیش رو بفرست (مثلاً @nameofgroup) تا امتحان کنم بشناسمت.\n\n"
            "نکته: لینک‌های دعوتِ خصوصی (مثل t.me/+...) رو هیچ رباتی نمی‌تونه باز کنه؛ این محدودیتِ خودِ تلگرامه."
        )


async def pv_chat_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """یه ورودیِ واحد برای هرچی که تو پیوی با «چت» شروع بشه - چون تلگرام برای هر پیام فقط
    یه هندلر رو تو یه گروه اجرا می‌کنه، همه‌ی حالت‌ها (ریپلای‌رو‌مدیا، خالی، جدا شو، متن) اینجا
    از هم تفکیک می‌شن، وگرنه بعضی حالت‌ها هیچ‌وقت اجرا نمی‌شدن."""
    msg = update.message
    rest = msg.text.strip()[len("چت"):].strip()

    # حالت ۱: ریپلای روی یه پیامِ مدیا (عکس/گیف/ویدیو) که خودمون قبلاً تو همین پیوی فرستادیم
    if msg.reply_to_message and (msg.reply_to_message.photo or msg.reply_to_message.animation or msg.reply_to_message.video):
        user_id = update.effective_user.id
        link = get_pv_link(user_id)
        if not link:
            await msg.reply_text("اول باید وصل بشی؛ فقط بنویس «چت» تا شروع کنیم.")
            return
        if not await is_user_admin(update, context, user_id, link["chat_id"]):
            remove_pv_link(user_id)
            await msg.reply_text(f"⛔ دیگه تو «{link['chat_title']}» ادمین نیستی، لینک قطع شد.")
            return
        media_msg = msg.reply_to_message
        asyncio.create_task(_safe_delete_message(context, update.effective_chat.id, msg.message_id))
        asyncio.create_task(_safe_delete_message(context, update.effective_chat.id, media_msg.message_id))
        await _pv_forward_media(update, context, link["chat_id"], link, media_msg, rest)
        return

    # حالت ۲: «چت جدا شو»
    if rest == "جدا شو":
        user_id = update.effective_user.id
        if get_pv_link(user_id):
            remove_pv_link(user_id)
            await msg.reply_text("🔌 لینک قطع شد.")
        else:
            await msg.reply_text("لینکی وصل نبود.")
        return

    # حالت ۳: فقط «چت» تنها - نمایشِ وضعیت / شروعِ لینک‌کردن
    if not rest:
        await _pv_chat_show_status(update, context)
        return

    # حالت ۴: «چت <متن>» - ارسالِ متن (با احتمالِ تگ) به گروهِ لینک‌شده
    user_id = update.effective_user.id
    link = get_pv_link(user_id)
    if not link:
        await msg.reply_text("اول باید وصل بشی؛ فقط بنویس «چت» تا شروع کنیم.")
        return
    if not await is_user_admin(update, context, user_id, link["chat_id"]):
        remove_pv_link(user_id)
        await msg.reply_text(f"⛔ دیگه تو «{link['chat_title']}» ادمین نیستی، لینک قطع شد.")
        return
    asyncio.create_task(_safe_delete_message(context, update.effective_chat.id, msg.message_id))
    await _pv_forward_text(update, context, link["chat_id"], link, rest)


async def pv_media_with_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عکس/گیف/ویدیو که مستقیماً تو پیوی با کپشنِ «چت ...» فرستاده شده."""
    msg = update.message
    caption = (msg.caption or "").strip()
    if not (caption == "چت" or caption.startswith("چت ")):
        return
    rest = caption[len("چت"):].strip()
    user_id = update.effective_user.id
    link = get_pv_link(user_id)
    if not link:
        await msg.reply_text("اول باید وصل بشی؛ تو پیوی فقط بنویس «چت» تا شروع کنیم.")
        return
    if not await is_user_admin(update, context, user_id, link["chat_id"]):
        remove_pv_link(user_id)
        await msg.reply_text(f"⛔ دیگه تو «{link['chat_title']}» ادمین نیستی، لینک قطع شد.")
        return
    asyncio.create_task(_safe_delete_message(context, update.effective_chat.id, msg.message_id))
    await _pv_forward_media(update, context, link["chat_id"], link, msg, rest)


async def pv_link_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[-1])
    user_id = update.effective_user.id
    if not await is_user_admin(update, context, user_id, chat_id):
        await query.answer("دیگه تو این گروه ادمین نیستی.", show_alert=True)
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT title FROM user_groups WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    title = (row["title"] if row and row["title"] else f"گروه {chat_id}")
    set_pv_link(user_id, chat_id, title)
    await query.answer()
    await query.edit_message_text(f"✅ وصل شد به «{title}».\nخب شروع کن: بنویس «چت <متن>» تا تو همون گروه پست بشه.")


# ---------------------------------------------------------------------------
# سوپر مالک - فقط برای دیدنِ فهرستِ همه‌ی گروه‌هایی که ربات توشونه (چک‌کردنِ اینکه
# جایی که نباید، ربات رو نبردن). این کاملاً جدا از SUPER_ADMIN_IDS و مالکِ خودِ هر گروهه.
# ---------------------------------------------------------------------------
async def super_owner_group_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return  # عمداً بی‌سروصدا - این قابلیت نباید لو بره
    chat_ids = get_all_group_chat_ids()
    if not chat_ids:
        await update.message.reply_text("ربات فعلاً تو هیچ گروهی نیست.")
        return

    lines = [f"📋 گروه‌های عضوِ ربات ({len(chat_ids)} تا):", ""]
    for i, chat_id in enumerate(chat_ids, start=1):
        try:
            chat = await context.bot.get_chat(chat_id)
            title = chat.title or f"گروه {chat_id}"
        except Exception:
            title = f"گروه {chat_id} (نشناختمش، شاید ربات دیگه توش نیست)"
        try:
            count = await context.bot.get_chat_member_count(chat_id)
            count_text = str(count)
        except Exception:
            count_text = "نامشخص"
        try:
            link = await context.bot.export_chat_invite_link(chat_id)
        except Exception:
            link = "لینک در دسترس نیست (ربات ادمین نیست یا دسترسیِ لازم رو نداره)"

        lines.append(f"{i}. {title}")
        lines.append(f"   👥 عضو: {count_text}")
        lines.append(f"   🔗 {link}")
        lines.append(f"   🆔 {chat_id}")
        lines.append("")

    # پیام‌های تلگرام سقفِ طول دارن؛ اگه لیست طولانی بود، چندتکه‌ای می‌فرستیم
    CHUNK_CHAR_LIMIT = 3500
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > CHUNK_CHAR_LIMIT:
            await update.message.reply_text(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        await update.message.reply_text(buf)


# ---------------------------------------------------------------------------
# پیام همگانی - فقط سوپرادمین‌ها (SUPER_ADMIN_IDS)، فقط از پیویِ ربات. عمداً محدود به
# سوپرادمین شده چون این قابلیت یه پیام رو به همه‌ی گروه‌های عضوِ ربات می‌فرسته و نباید
# دستِ هر ادمینِ یه گروه باشه.
# ---------------------------------------------------------------------------
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text(
        "📢 حالت پیام همگانی.\n"
        "هرچی الان بفرستی (متن، عکس، گیف، فیلم، هرچی) دقیقاً همون‌جوری به همه‌ی گروه‌هایی که توشونم فرستاده می‌شه.\n"
        "برای انصراف /cancel رو بزن."
    )
    return BROADCAST_WAIT_CONTENT


async def broadcast_got_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return ConversationHandler.END
    msg = update.message
    chat_ids = get_all_group_chat_ids()
    if not chat_ids:
        await msg.reply_text("فعلاً تو هیچ گروهی نیستم؛ چیزی برای فرستادن نبود.")
        return ConversationHandler.END

    status = await msg.reply_text(f"⏳ در حال فرستادن به {len(chat_ids)} گروه...")
    sent_count, failed_count = 0, 0
    for chat_id in chat_ids:
        try:
            copied = await context.bot.copy_message(chat_id=chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
            track_message(chat_id, copied.message_id)
            sent_count += 1
        except Exception:
            failed_count += 1
        await asyncio.sleep(0.05)  # یه‌کم فاصله بین پیام‌ها تا محدودیتِ ریت تلگرام گیر نکنیم

    await status.edit_text(f"✅ پیام همگانی فرستاده شد.\nموفق: {sent_count} گروه\nناموفق: {failed_count} گروه")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# پنلِ مخصوصِ سوپرمالک - کاملاً مخفی، فقط با فرستادنِ عبارتِ دقیقِ «پنل مخصوص» تو پیویِ ربات
# باز می‌شه، و فقط اگه فرستنده خودِ SUPER_OWNER_ID باشه. برای هرکسِ دیگه، انگار همچین چیزی
# اصلاً وجود نداره (هیچ پاسخی داده نمی‌شه) تا کسی از وجودش خبردار نشه.
# ---------------------------------------------------------------------------
def get_global_bot_enabled() -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT bot_enabled FROM super_panel_settings WHERE id=1")
    row = c.fetchone()
    conn.close()
    return bool(row["bot_enabled"]) if row else True


def set_global_bot_enabled(enabled: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO super_panel_settings (id, bot_enabled) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET bot_enabled=excluded.bot_enabled",
        (1 if enabled else 0,),
    )
    conn.commit()
    conn.close()


def is_channel_banned(user_id: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM channel_banned_users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


def add_channel_ban(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM channel_banned_users WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO channel_banned_users (user_id, banned_at) VALUES (?, ?)", (user_id, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def remove_channel_ban(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM channel_banned_users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def list_channel_bans():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, banned_at FROM channel_banned_users ORDER BY banned_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows


MATH_STATS_PAGE_SIZE = 10


def get_global_math_stats_page(offset: int, limit: int = MATH_STATS_PAGE_SIZE):
    """آمارِ سراسریِ همه‌ی کسایی که تا حالا چالشِ ریاضی جواب دادن (نه فقط یه گروهِ خاص)،
    صفحه‌بندی‌شده. اسمِ نمایشی رو از آخرین باری که تو هر گروهی دیده شدن برمی‌داریم."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT usl.user_id, usl.correct_count, COALESCE(ul.xp, 0) AS xp, "
        "(SELECT gm.first_name FROM group_members gm WHERE gm.user_id = usl.user_id "
        " ORDER BY gm.updated_at DESC LIMIT 1) AS first_name "
        "FROM user_subject_levels usl "
        "LEFT JOIN user_levels ul ON ul.user_id = usl.user_id "
        "WHERE usl.subject = ? "
        "ORDER BY usl.correct_count DESC "
        "LIMIT ? OFFSET ?",
        ("ریاضی", limit, offset),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_global_math_stats_count() -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS cnt FROM user_subject_levels WHERE subject=?", ("ریاضی",))
    row = c.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def super_panel_keyboard():
    enabled = get_global_bot_enabled()
    status = "✅ روشن" if enabled else "⛔ خاموش"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"ربات کلاً: {status}", callback_data="superpanel_toggle_bot")],
            [InlineKeyboardButton("➕➖ افزودن/کم‌کردنِ لولِ یه کاربر", callback_data="superpanel_xp_start")],
            [InlineKeyboardButton("🚫 بن‌کردنِ کاربر از کانال", callback_data="superpanel_channel_ban_menu")],
            [InlineKeyboardButton("📊 آمار ریاضی", callback_data="superpanel_mathstats_0")],
            [InlineKeyboardButton("❌ بستن", callback_data="superpanel_close")],
        ]
    )


async def super_panel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return  # سکوتِ کامل؛ حتی یه پیامِ ردِ دسترسی هم نمی‌فرستیم که وجودش لو نره
    await update.message.reply_text(
        f"🛡 پنلِ مخصوصِ سوپرمالک (نسخه‌ی {BOT_VERSION})", reply_markup=super_panel_keyboard()
    )


async def superpanel_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    try:
        await update.callback_query.answer()
        await update.callback_query.message.delete()
    except Exception:
        pass


# ---- زیرمنوی بن‌کردنِ کانال: لیست + بن‌کردنِ جدید ---------------------------
async def superpanel_channel_ban_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    await update.callback_query.answer()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 لیستِ بن‌شده‌ها", callback_data="superpanel_channel_ban_list")],
            [InlineKeyboardButton("➕ بن‌کردنِ کاربرِ جدید", callback_data="superpanel_channel_ban_start")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="superpanel_back_main")],
        ]
    )
    await update.callback_query.edit_message_text("🚫 بن‌کردنِ کاربر از کانال", reply_markup=keyboard)


async def superpanel_back_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"🛡 پنلِ مخصوصِ سوپرمالک (نسخه‌ی {BOT_VERSION})", reply_markup=super_panel_keyboard()
    )


async def superpanel_channel_ban_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    await update.callback_query.answer()
    rows = list_channel_bans()
    buttons = []
    if not rows:
        text = "فعلاً هیچ‌کس تو لیستِ بن‌شده‌های کانال نیست."
    else:
        lines = ["📋 کاربرهای بن‌شده از کانال:", "", "برای آزادکردن، رو هرکدوم بزن:"]
        for row in rows:
            buttons.append([InlineKeyboardButton(f"🔓 آزادکردنِ {row['user_id']}", callback_data=f"superpanel_channel_unban_{row['user_id']}")])
        text = "\n".join(lines)
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="superpanel_channel_ban_menu")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def superpanel_channel_unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    user_id = int(update.callback_query.data.split("_")[-1])
    remove_channel_ban(user_id)
    if REQUIRED_CHANNEL_ID:
        try:
            await context.bot.unban_chat_member(REQUIRED_CHANNEL_ID, user_id, only_if_banned=True)
        except Exception:
            pass
    await update.callback_query.answer(f"کاربر {user_id} آزاد شد.")
    await superpanel_channel_ban_list_callback(update, context)


# ---- آمار ریاضیِ سراسری (صفحه‌بندی‌شده، رو همون پیام ادیت می‌شه) -------------
async def superpanel_mathstats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    await update.callback_query.answer()
    offset = int(update.callback_query.data.split("_")[-1])
    total = get_global_math_stats_count()
    rows = get_global_math_stats_page(offset)

    if not rows:
        text = "هنوز هیچ‌کس تو هیچ گروهی چالشِ ریاضی جواب نداده."
        buttons = [[InlineKeyboardButton("⬅️ بازگشت", callback_data="superpanel_back_main")]]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    page_num = (offset // MATH_STATS_PAGE_SIZE) + 1
    total_pages = max(1, (total + MATH_STATS_PAGE_SIZE - 1) // MATH_STATS_PAGE_SIZE)
    lines = [f"📊 آمارِ سراسریِ ریاضی (صفحه‌ی {page_num} از {total_pages})", ""]
    for i, row in enumerate(rows, start=offset + 1):
        name = html.escape(row["first_name"] or f"کاربر {row['user_id']}")
        overall_level, is_legendary = level_from_xp(row["xp"])
        overall_label = "افسانه‌ای" if is_legendary else f"سطح {overall_level}"
        math_level = subject_level_from_count(row["correct_count"])
        lines.append(f"{i}. {name} (#{row['user_id']}) — {row['correct_count']} جواب | لولِ اصلی: {overall_label} | ریاضی: سطح {math_level}")

    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton("⬅️ برگشت", callback_data=f"superpanel_mathstats_{max(0, offset - MATH_STATS_PAGE_SIZE)}"))
    if offset + MATH_STATS_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("صفحه‌ی بعد ➡️", callback_data=f"superpanel_mathstats_{offset + MATH_STATS_PAGE_SIZE}"))
    buttons = []
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton("⬅️ بازگشت به پنل", callback_data="superpanel_back_main")])

    await update.callback_query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def superpanel_toggle_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return
    set_global_bot_enabled(not get_global_bot_enabled())
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"🛡 پنلِ مخصوصِ سوپرمالک (نسخه‌ی {BOT_VERSION})", reply_markup=super_panel_keyboard()
    )


async def superpanel_xp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "آیدیِ عددیِ کاربر رو بفرست، یا یه پیام ازش رو همینجا فوروارد کن.\nبرای انصراف /cancel."
    )
    return SUPERPANEL_XP_WAIT_USER


async def superpanel_xp_got_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return ConversationHandler.END
    msg = update.message
    target_id = None
    if msg.forward_origin is not None and getattr(msg.forward_origin, "sender_user", None) is not None:
        target_id = msg.forward_origin.sender_user.id
    elif msg.text and msg.text.strip().lstrip("-").isdigit():
        target_id = int(msg.text.strip())
    if target_id is None:
        await msg.reply_text("نتونستم آیدی رو تشخیص بدم. یا آیدیِ عددی بفرست، یا یه پیام ازش فوروارد کن.")
        return SUPERPANEL_XP_WAIT_USER
    context.user_data["superpanel_target_id"] = target_id
    await msg.reply_text(
        f"باشه، کاربر: {target_id}\nحالا بگو چندتا لول (XP) اضافه بشه. برای کم‌کردن، عددِ منفی بفرست (مثلاً -20)."
    )
    return SUPERPANEL_XP_WAIT_AMOUNT


async def superpanel_xp_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return ConversationHandler.END
    target_id = context.user_data.pop("superpanel_target_id", None)
    if target_id is None:
        await update.message.reply_text("یه مشکلی پیش اومد، دوباره از اول امتحان کن.")
        return ConversationHandler.END
    text = update.message.text.strip()
    try:
        amount = int(text)
    except ValueError:
        context.user_data["superpanel_target_id"] = target_id
        await update.message.reply_text("این عدد نبود! یه عددِ صحیح بفرست (مثبت یا منفی).")
        return SUPERPANEL_XP_WAIT_AMOUNT
    old_xp, new_xp = add_xp(target_id, amount)
    old_level, _ = level_from_xp(old_xp)
    new_level, is_legendary = level_from_xp(new_xp)
    await update.message.reply_text(
        f"✅ انجام شد.\nکاربر {target_id}: از {old_xp} لول به {new_xp} لول رسید "
        f"(از سطح {old_level} به سطح {'افسانه‌ای' if is_legendary else new_level})."
    )
    return ConversationHandler.END


async def superpanel_channel_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return ConversationHandler.END
    if not REQUIRED_CHANNEL_ID:
        await update.callback_query.answer("هنوز REQUIRED_CHANNEL_ID تنظیم نشده.", show_alert=True)
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "آیدیِ عددیِ کاربری که می‌خوای از کانال بن بشه رو بفرست، یا یه پیام ازش فوروارد کن.\n"
        "هروقت بعداً بخواد بیاد تو کانال، خودکار بیرونش می‌کنیم.\nبرای انصراف /cancel."
    )
    return SUPERPANEL_CHANNEL_BAN_WAIT_USER


async def superpanel_channel_ban_got_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_owner(update.effective_user.id):
        return ConversationHandler.END
    msg = update.message
    target_id = None
    if msg.forward_origin is not None and getattr(msg.forward_origin, "sender_user", None) is not None:
        target_id = msg.forward_origin.sender_user.id
    elif msg.text and msg.text.strip().lstrip("-").isdigit():
        target_id = int(msg.text.strip())
    if target_id is None:
        await msg.reply_text("نتونستم آیدی رو تشخیص بدم. یا آیدیِ عددی بفرست، یا یه پیام ازش فوروارد کن.")
        return SUPERPANEL_CHANNEL_BAN_WAIT_USER

    add_channel_ban(target_id)
    already_kicked = False
    try:
        await context.bot.ban_chat_member(REQUIRED_CHANNEL_ID, target_id)
        already_kicked = True
    except Exception:
        pass

    note = " (همین الان هم عضو بود، بیرونش کردم.)" if already_kicked else " (فعلاً عضو نبود؛ هروقت بیاد خودکار بیرون میفته.)"
    await msg.reply_text(f"✅ کاربر {target_id} تو لیستِ بن‌شده‌های کانال ثبت شد.{note}")
    return ConversationHandler.END


async def global_kill_switch_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اگه سوپرمالک ربات رو کلاً خاموش کرده باشه، هیچ‌کس دیگه (حتی ادمین‌ها) نمی‌تونه باهاش
    تعامل کنه، تا خودِ سوپرمالک دوباره روشنش کنه. این هندلر باید اولین چیزی باشه که اجرا می‌شه."""
    user = update.effective_user
    if user is not None and is_super_owner(user.id):
        return
    if not get_global_bot_enabled():
        raise ApplicationHandlerStop


async def pv_awaiting_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی تو پیوی یه چیزِ شروع‌شده با @ می‌فرستن - امتحان می‌کنیم به‌عنوان یوزرنیمِ گروه بشناسیمش."""
    text = update.message.text.strip()
    if not text.startswith("@"):
        return
    user_id = update.effective_user.id
    try:
        chat = await context.bot.get_chat(text)
    except Exception:
        await update.message.reply_text("نشناختمش؛ مطمئن شو یوزرنیمِ درستِ یه گروهِ عمومیه که من توشم.")
        return
    if not _group_settings_exists(chat.id):
        await update.message.reply_text("این گروه رو نمی‌شناسم (باید من عضوش باشم).")
        return
    if not await is_user_admin(update, context, user_id, chat.id):
        await update.message.reply_text("تو این گروه ادمین نیستی.")
        return
    title = chat.title or text
    set_pv_link(user_id, chat.id, title)
    await update.message.reply_text(f"✅ شناختمت! وصل شدی به «{title}».\nخب شروع کن: بنویس «چت <متن>».")


async def pv_calculator_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تو پیوی: «حساب <عبارت>» یا حتی مستقیم خودِ عبارت (مثلاً فقط «۵+۳») رو حساب می‌کنه."""
    text = update.message.text.strip()
    if text.startswith("حساب"):
        expr = text[len("حساب"):].strip()
    else:
        expr = text
    ok, result = calculate_expression(expr)
    if ok:
        await update.message.reply_text(f"🧮 {format_calc_result(result)}")
    else:
        await update.message.reply_text("این سوال رو بلد نیستم.")


async def group_media_say_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تو گروه: عکس/گیف/ویدیو با کپشنِ دقیقاً «بگو» - همون مدیا رو از طرفِ ربات دوباره می‌فرسته."""
    msg = update.message
    if (msg.caption or "").strip() != "بگو":
        return
    chat_id = update.effective_chat.id
    reply_target = msg.reply_to_message.message_id if msg.reply_to_message else None
    try:
        if msg.photo:
            sent = await context.bot.send_photo(chat_id, msg.photo[-1].file_id, reply_to_message_id=reply_target)
        elif msg.animation:
            sent = await context.bot.send_animation(chat_id, msg.animation.file_id, reply_to_message_id=reply_target)
        elif msg.video:
            sent = await context.bot.send_video(chat_id, msg.video.file_id, reply_to_message_id=reply_target)
        else:
            return
        track_message(chat_id, sent.message_id)
    except Exception:
        pass




OWNER_PROTECTED_TEXT = (
    "🛡 داداش {name} مالکِ گروهه، فعلاً دستم بهش نمی‌رسه.\n"
    "اگه می‌خوای این محافظت رو برداری: پنل ادمین ⚙️ تنظیمات گروه ← «اعمال روی مالک» رو روشن کن.\n"
    "(یه نکته: حتی با روشن‌کردنش، خودِ تلگرام هیچ‌وقت اجازه‌ی سکوت/بن کردنِ مالکِ واقعیِ گروه رو نمی‌ده - این محدودیتِ خودِ پلتفرمه، دستِ من نیست.)"
)


async def unmute_member(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """
    آزادسازی صحیح از سکوت: به‌جای فرض‌کردنِ ماکزیممِ مجوزها، مجوزهای پیش‌فرضِ همون گروه رو
    می‌گیره و همونا رو به کاربر برمی‌گردونه (رفتار درست‌تر و کمتر خطاده). برمی‌گردونه: (True, None) یا (False, exception).
    """
    try:
        chat = await context.bot.get_chat(chat_id)
        default_perms = chat.permissions
        if default_perms is not None:
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=default_perms)
        else:
            raise ValueError("no default permissions")
        return True, None
    except Exception:
        pass
    # اگه گرفتنِ مجوزهای پیش‌فرض گروه شکست خورد، با یه ست کامل و امن دوباره امتحان می‌کنیم
    try:
        full_permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        )
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=full_permissions)
        return True, None
    except Exception:
        pass
    # آخرین تلاش: فقط حداقلیِ لازم (اجازه‌ی ارسال پیام) تا حداقل صداش دربیاد
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=True))
        return True, None
    except Exception as e3:
        return False, e3


def explain_admin_action_error(name: str, exc) -> str:
    """پیام خام تلگرام رو به یه توضیح فارسیِ قابل‌فهم تبدیل می‌کنه (مخصوصا برای موردِ ادمین/مالک بودنِ هدف)."""
    err_text = str(exc) if exc else ""
    low = err_text.lower()
    if "admin" in low or "owner" in low or "creator" in low:
        return (
            f"نشد داداش، رو {name} این کار جواب نمی‌ده: تلگرام اجازه نمی‌ده رو یکی که خودش ادمین یا مالکِ گروهه "
            f"دست ببریم (مگه اینکه خودِ همین ربات ادمینش کرده باشه). این محدودیتِ خودِ تلگرامه، دستِ من نیست."
        )
    if "not enough rights" in low or "chat_admin_required" in low:
        return (
            "این کارو نتونستم بکنم؛ فکر کنم من (ربات) دسترسیِ ادمینیِ لازم رو تو این گروه ندارم. "
            "یه سر به تنظیماتِ ادمینیِ ربات تو گروه بزن."
        )
    return f"رو {name} جواب نداد داداش: {err_text}"


URL_PATTERN = re.compile(r"(https?://|t\.me/|www\.)\S+", re.IGNORECASE)


async def adm_warn_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text(
        "روی پیام عضو مورد نظر ریپلای کن و دلیل اخطار رو بنویس."
    )
    return WARN_WAIT_REASON


async def adm_warn_add_got_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("باید روی پیام همون عضو ریپلای کنی. دوباره امتحان کن.")
        return WARN_WAIT_REASON
    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    reason = update.message.text
    await perform_warn(context, chat_id, target, reason, reply_target_message=update.message)
    return ConversationHandler.END


async def adm_warn_remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text(
        "روی پیام عضوی که می‌خوای اخطارش پاک بشه ریپلای کن و هر متنی بفرست."
    )
    return REMOVE_WARN_WAIT_USER


async def adm_warn_remove_got_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("باید روی پیام همون عضو ریپلای کنی. دوباره امتحان کن.")
        return REMOVE_WARN_WAIT_USER
    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    remove_all_warnings(chat_id, target.id)
    await update.message.reply_text(f"✅ اخطارهای {target.first_name} پاک شد.")
    return ConversationHandler.END


async def warn_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = int(query.data.split("_")[-1])
    chat_id = update.effective_chat.id
    reasons = get_warning_reasons(chat_id, user_id)
    if not reasons:
        await query.answer("اخطاری برای این کاربر ثبت نشده (یا قبلا پاک شده).", show_alert=True)
        return
    text = "دلایل اخطار:\n" + "\n".join(f"- {r}" for r in reasons)
    await query.answer(text[:200], show_alert=True)  # تلگرام برای پاپ‌آپِ آلرت حداکثر ۲۰۰ کاراکتر اجازه می‌ده


async def warn_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_user_admin(update, context, user_id, chat_id):
        await query.answer("⛔ پاک‌کردنِ اخطار کارِ ادمیناست داداش.", show_alert=True)
        return
    await query.answer()
    target_id = int(query.data.split("_")[-1])
    remove_all_warnings(chat_id, target_id)
    await query.message.reply_text("✅ اخطار این کاربر پاک شد.")


# ---- کلمات ممنوعه ------------------------------------------------------------
async def adm_forbidden_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    words = list_forbidden_words(chat_id)
    text = "کلمات ممنوعه فعلی:\n" + (", ".join(words) if words else "هیچ کلمه‌ای ثبت نشده")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ اضافه کردن کلمه", callback_data="forb_add")],
            [InlineKeyboardButton("➖ حذف کلمه", callback_data="forb_remove")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")],
        ]
    )
    await update.callback_query.edit_message_text(text, reply_markup=keyboard)


async def forb_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text("کلمه‌ای که می‌خوای ممنوع بشه رو بفرست.")
    return FORBIDDEN_ADD_WAIT_WORD


async def forb_add_got_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_forbidden_word(update.effective_chat.id, update.message.text)
    await update.message.reply_text(f"✅ «{update.message.text}» به لیست کلمات ممنوعه اضافه شد.")
    return ConversationHandler.END


async def forb_remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text("کلمه‌ای که می‌خوای از لیست ممنوعه حذف بشه رو بفرست.")
    return FORBIDDEN_REMOVE_WAIT_WORD


async def forb_remove_got_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_forbidden_word(update.effective_chat.id, update.message.text)
    await update.message.reply_text(f"✅ «{update.message.text}» از لیست کلمات ممنوعه حذف شد.")
    return ConversationHandler.END


# ---- حذف پیام‌ها --------------------------------------------------------------
async def adm_delete_msgs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 حذف کل پیام‌های ردیابی‌شده گروه", callback_data="del_all")],
            [InlineKeyboardButton("🔢 حذف با عدد دلخواه", callback_data="del_n")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")],
        ]
    )
    await update.callback_query.edit_message_text("از گزینه‌های زیر استفاده کن:", reply_markup=keyboard)


async def del_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    message_ids = get_tracked_messages(chat_id)
    deleted = 0
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    clear_tracked_messages(chat_id, message_ids)
    await update.callback_query.edit_message_text(
        f"✅ {deleted} پیام حذف شد (فقط پیام‌هایی که ربات از زمان اضافه شدنش دیده بود قابل حذف هستن).",
        reply_markup=back_to_admin_keyboard(),
    )


async def del_n_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text("می‌خوای چند تا پیام پاک بشه؟ یه عدد بفرست.")
    context.user_data["del_n_prompt_msg_id"] = update.callback_query.message.message_id
    return DELETE_N_WAIT_NUMBER


async def _self_cleanup_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list, delay: float = 4.0):
    """بعد از یه مکثِ کوتاه (تا ادمین جواب رو ببینه)، خودِ پیام‌های مربوط به این عملیات
    (پرامپت، جواب ادمین، تاییدیه) رو پاک می‌کنه تا تو گروه ردی ازشون نمونه."""
    await asyncio.sleep(delay)
    for mid in message_ids:
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def del_n_got_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("لطفا فقط یه عدد بفرست.")
        return DELETE_N_WAIT_NUMBER
    if n <= 0:
        await update.message.reply_text("یه عدد مثبت بفرست.")
        return DELETE_N_WAIT_NUMBER
    chat_id = update.effective_chat.id

    # نکته‌ی مهم: بعضی از پیام‌های ردیابی‌شده ممکنه دیگه قابل‌حذف نباشن (مثلاً قبلاً پاک شده باشن)،
    # پس اگه فقط دقیقاً n تا آیدی امتحان کنیم و چندتاش شکست بخوره، کمتر از عددی که خواسته بودی
    # پاک می‌شه. اینجا یه بافرِ اضافه هم می‌گیریم و تا وقتی واقعاً n تا با موفقیت پاک نشه (یا
    # دیگه آیدی‌ای برای امتحان نمونه) ادامه می‌دیم.
    candidates = get_tracked_messages(chat_id, limit=n + 50)
    deleted = 0
    consumed_ids = []
    for mid in candidates:
        consumed_ids.append(mid)
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
        if deleted >= n:
            break
    clear_tracked_messages(chat_id, consumed_ids)
    confirm = await update.message.reply_text(f"✅ {deleted} پیام پاک شد.")

    # این سه‌تا پیامِ خودِ فرآیند (پرامپت، عددی که ادمین فرستاد، همین تاییدیه) ربطی به
    # شمارشِ بالا ندارن - بعد چند ثانیه خودشون هم خودکار پاک می‌شن تا گروه شلوغ نمونه
    cleanup_ids = [update.message.message_id, confirm.message_id]
    prompt_id = context.user_data.pop("del_n_prompt_msg_id", None)
    if prompt_id:
        cleanup_ids.append(prompt_id)
    asyncio.create_task(_self_cleanup_later(context, chat_id, cleanup_ids))
    return ConversationHandler.END


# ---- افراد ویژه ---------------------------------------------------------------
def _special_display_name(chat_id: int, user_id: int) -> str:
    rec = get_member_record(chat_id, user_id)
    if rec and rec["first_name"]:
        return rec["first_name"]
    return str(user_id)


async def adm_special_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    members = list_special_members(chat_id)
    buttons = []
    if not members:
        text = "در حال حاضر عضو ویژه‌ای ثبت نشده.\nمی‌خوای کسی رو عضو ویژه کنی؟"
    else:
        lines = ["🌟 اعضای ویژه:", ""]
        for m in members:
            name = _special_display_name(chat_id, m["user_id"])
            mode_label = "معافیت کامل" if m["mode"] == "all" else "دستی"
            lines.append(f"• {name} ({mode_label})" + (f" — لحن: {m['custom_tone']}" if m["custom_tone"] else ""))
            buttons.append([InlineKeyboardButton(f"⚙️ مدیریت {name}", callback_data=f"special_manage_{m['user_id']}")])
        text = "\n".join(lines)
    buttons.append([InlineKeyboardButton("➕ اضافه کردن عضو ویژه", callback_data="special_add")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def special_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text(
        "روی پیام عضوی که می‌خوای ویژه بشه ریپلای کن و هر متنی بفرست.\n"
        "(یا هر زمان که خواستی، تو خودِ گروه روی پیامش ریپلای کن و بنویس «کولیبا ویژه»)"
    )
    return SPECIAL_ADD_WAIT_USER


async def special_add_got_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("باید روی پیام همون عضو ریپلای کنی. دوباره امتحان کن.")
        return SPECIAL_ADD_WAIT_USER
    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    add_special_member(chat_id, target.id)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚙️ تنظیم دقیقِ قابلیت‌هاش", callback_data=f"special_manage_{target.id}")],
            [InlineKeyboardButton("🎨 لحن جدا براش درست کن", callback_data=f"special_tone_{target.id}")],
        ]
    )
    await update.message.reply_text(
        f"✅ {target.first_name} عضو ویژه شد و فعلاً از همه‌چیز (عکس/فیلم/استیکر/فوروارد/لینک/کلمات ممنوعه/اسپم) کاملاً معافه.\n"
        "می‌خوای تنظیمش کنی؟",
        reply_markup=keyboard,
    )
    return ConversationHandler.END


async def special_tone_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    user_id = int(query.data.split("_")[-1])
    context.user_data["special_tone_user"] = user_id
    await query.edit_message_text("متن لحن مخصوص این عضو رو بفرست (این متن فقط برای یادداشت شماست).")
    return SPECIAL_TONE_WAIT_TEXT


async def special_tone_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = context.user_data.pop("special_tone_user", None)
    if user_id is None:
        await update.message.reply_text("مشکلی پیش اومد، دوباره امتحان کن.")
        return ConversationHandler.END
    set_special_tone(update.effective_chat.id, user_id, update.message.text)
    await update.message.reply_text("✅ لحن ثبت شد.")
    return ConversationHandler.END


# ---- مدیریتِ دقیقِ قابلیت‌های هر عضو ویژه (تنظیم کل / تنظیم دستی) -------------
async def special_manage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    user_id = int(update.callback_query.data.split("_")[-1])
    row = get_special_member(chat_id, user_id)
    if row is None:
        await update.callback_query.answer("این فرد دیگه عضو ویژه نیست.", show_alert=True)
        await adm_special_menu(update, context)
        return
    name = _special_display_name(chat_id, user_id)
    buttons = [
        [
            InlineKeyboardButton(
                f"{'🔘' if row['mode'] == 'all' else '⚪️'} تنظیم کل (معافیت کامل)",
                callback_data=f"special_mode_all_{user_id}",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'🔘' if row['mode'] == 'custom' else '⚪️'} تنظیم دستی",
                callback_data=f"special_mode_custom_{user_id}",
            )
        ],
    ]
    text = f"⚙️ مدیریتِ {name}\n\nحالتِ فعلی: {'تنظیم کل' if row['mode'] == 'all' else 'تنظیم دستی'}"
    if row["mode"] == "custom":
        text += "\n\nهر کدوم رو بزنی، روشن/خاموش می‌شه (✅ یعنی این فرد از این محدودیت معافه):"
        for feature, column in SPECIAL_FEATURE_COLUMNS.items():
            status = "✅" if row[column] else "❌"
            buttons.append(
                [InlineKeyboardButton(f"{SPECIAL_FEATURE_LABELS[feature]}: {status}", callback_data=f"special_toggle_{feature}_{user_id}")]
            )
    buttons.append([InlineKeyboardButton("🎨 لحن جدا", callback_data=f"special_tone_{user_id}")])
    buttons.append([InlineKeyboardButton("🗑 حذف از ویژه", callback_data=f"special_remove_{user_id}")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_special")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def special_mode_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    user_id = int(update.callback_query.data.split("_")[-1])
    set_special_mode(update.effective_chat.id, user_id, "all")
    await special_manage_menu(update, context)


async def special_mode_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    user_id = int(update.callback_query.data.split("_")[-1])
    set_special_mode(update.effective_chat.id, user_id, "custom")
    await special_manage_menu(update, context)


async def special_toggle_feature_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    parts = update.callback_query.data.split("_")  # special_toggle_<feature>_<user_id>
    feature = parts[2]
    user_id = int(parts[3])
    toggle_special_feature(update.effective_chat.id, user_id, feature)
    await special_manage_menu(update, context)


async def special_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    user_id = int(update.callback_query.data.split("_")[-1])
    remove_special_member(update.effective_chat.id, user_id)
    await update.callback_query.answer("از لیست ویژه‌ها درش آوردم.")
    await adm_special_menu(update, context)


# ---- لیست سکوت‌شده‌ها (تو پنل ادمین) ---------------------------------------
def _format_remaining(until_ts) -> str:
    if not until_ts:
        return "نامحدود (تا وقتی خودتون آزادش کنید)"
    remaining = until_ts - int(datetime.utcnow().timestamp())
    if remaining <= 0:
        return "به‌زودی آزاد می‌شه"
    hours, rem = divmod(remaining, 3600)
    minutes = rem // 60
    if hours >= 1:
        return f"{int(hours)} ساعت و {int(minutes)} دقیقه مونده"
    return f"{int(minutes)} دقیقه مونده"


async def adm_mutelist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    rows = list_active_mutes(chat_id)
    buttons = []
    if not rows:
        text = "فعلاً هیچ‌کس تو این گروه سکوت نیست. 🔊"
    else:
        lines = ["🔇 لیست سکوت‌شده‌های فعلی:", ""]
        for r in rows[:25]:
            name = r["user_name"] or "کاربر"
            lines.append(f"• {name} — {_format_remaining(r['until_ts'])}")
            buttons.append([InlineKeyboardButton(f"🔊 آزاد کردن {name}", callback_data=f"adm_unmute_{r['user_id']}")])
        if len(rows) > 25:
            lines.append(f"\n... و {len(rows) - 25} نفر دیگه.")
        text = "\n".join(lines)
    buttons.append([InlineKeyboardButton("🔄 تازه‌سازی", callback_data="adm_mutelist")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def adm_unmute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    user_id = int(update.callback_query.data.split("_")[-1])
    ok, err = await unmute_member(context, chat_id, user_id)
    if ok:
        remove_mute_record(chat_id, user_id)
        await update.callback_query.answer("آزاد شد 🔊")
    else:
        remove_mute_record(chat_id, user_id)  # اگه دیگه سکوت واقعی‌ای در کار نبود، از لیست پاکش کن
        await update.callback_query.answer("آزادش کردم از لیست (شاید از قبل دیگه سکوت نبوده).", show_alert=True)
    await adm_mutelist_menu(update, context)


# ---- لیست مسدودی‌ها (تو پنل ادمین) -----------------------------------------
async def adm_banlist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    rows = list_banned(chat_id)
    buttons = []
    if not rows:
        text = "فعلاً هیچ‌کس از این گروه مسدود نیست. 🚫🙅"
    else:
        lines = ["🚫 لیست مسدودشده‌های فعلی:", ""]
        for r in rows[:25]:
            name = r["user_name"] or "کاربر"
            lines.append(f"• {name}" + (f" — {r['reason']}" if r["reason"] else ""))
            buttons.append([InlineKeyboardButton(f"🔓 آزاد کردن {name}", callback_data=f"adm_unban_{r['user_id']}")])
        if len(rows) > 25:
            lines.append(f"\n... و {len(rows) - 25} نفر دیگه.")
        text = "\n".join(lines)
        buttons.append([InlineKeyboardButton("🔓 آزاد کردن همه", callback_data="adm_unban_all")])
    buttons.append([InlineKeyboardButton("🔄 تازه‌سازی", callback_data="adm_banlist")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def adm_unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    user_id = int(update.callback_query.data.split("_")[-1])
    try:
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await update.callback_query.answer("آزاد شد 🔓")
    except Exception:
        await update.callback_query.answer("آزادش کردم از لیست (شاید از قبل دیگه بن نبوده).", show_alert=True)
    remove_ban_record(chat_id, user_id)
    await adm_banlist_menu(update, context)


async def adm_unban_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    rows = list_banned(chat_id)
    for r in rows:
        try:
            await context.bot.unban_chat_member(chat_id, r["user_id"], only_if_banned=True)
        except Exception:
            pass
        remove_ban_record(chat_id, r["user_id"])
    await update.callback_query.answer(f"{len(rows)} نفر آزاد شدن 🔓")
    await adm_banlist_menu(update, context)


# ---- تنظیمات گروه ---------------------------------------------------------------
def _toggle_col(s, col_name: str) -> str:
    return "✅ مجاز" if s[col_name] else "❌ ممنوع"


async def adm_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    link_status = "❌ ممنوع" if s["block_links"] else "✅ مجاز"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"فوروارد پیام: {_toggle_col(s, 'forward_allowed')}", callback_data="set_toggle_forward")],
            [InlineKeyboardButton(f"عکس: {_toggle_col(s, 'allow_photo')}", callback_data="set_toggle_photo")],
            [InlineKeyboardButton(f"فیلم: {_toggle_col(s, 'allow_video')}", callback_data="set_toggle_video")],
            [InlineKeyboardButton(f"استیکر و گیف: {_toggle_col(s, 'allow_sticker_gif')}", callback_data="set_toggle_sticker")],
            [InlineKeyboardButton(f"لینک (اعضای عادی): {link_status}", callback_data="set_toggle_links")],
            [InlineKeyboardButton(f"👋 پیام خوش‌آمد/خداحافظ: {_toggle_col(s, 'welcome_messages_enabled')}", callback_data="welcome_menu")],
            [InlineKeyboardButton(
                f"👑 اعمال روی مالک: {'✅ فعال' if s['admin_actions_on_owner'] else '❌ غیرفعال'}",
                callback_data="set_toggle_owner_actions",
            )],
            [InlineKeyboardButton("🚨 اسپم", callback_data="set_spam_menu")],
            [InlineKeyboardButton(f"🧮 چالش‌های سرگرمی: {_toggle_col(s, 'challenges_enabled')}", callback_data="challenge_menu")],
            [InlineKeyboardButton("💌 یادآوری غیبت اعضا", callback_data="eng_menu")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")],
        ]
    )
    await update.callback_query.edit_message_text("تنظیمات گروه، هر مورد رو می‌تونی روشن/خاموش یا تنظیم کنی:", reply_markup=keyboard)


async def _toggle_setting_and_refresh(update, context, column: str):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    new_val = 0 if s[column] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE group_settings SET {column}=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    await adm_settings_menu(update, context)


def _format_challenge_interval(seconds: int) -> str:
    seconds = seconds or CHALLENGE_DEFAULT_INTERVAL_SECONDS
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} ساعت"
    return f"{seconds // 60} دقیقه"


async def challenge_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    status = "✅ روشن" if s["challenges_enabled"] else "❌ خاموش"
    current_interval = _format_challenge_interval(s["challenge_interval_seconds"])
    current_difficulty = s["challenge_difficulty"] or "normal"

    buttons = [[InlineKeyboardButton(f"وضعیت: {status}", callback_data="set_toggle_challenges")]]
    minute_row = [InlineKeyboardButton(f"{m} دقیقه", callback_data=f"challenge_interval_{m * 60}") for m in (10, 15, 20, 30)]
    buttons.append(minute_row)
    hour_options = (1, 2, 3, 6, 12, 24)
    hour_row = []
    for h in hour_options:
        hour_row.append(InlineKeyboardButton(f"{h} ساعت", callback_data=f"challenge_interval_{h * 3600}"))
        if len(hour_row) == 3:
            buttons.append(hour_row)
            hour_row = []
    if hour_row:
        buttons.append(hour_row)

    difficulty_row = []
    for key in CHALLENGE_DIFFICULTIES:
        label = CHALLENGE_DIFFICULTY_LABELS[key]
        mark = "🔘" if key == current_difficulty else "⚪️"
        difficulty_row.append(InlineKeyboardButton(f"{mark} {label}", callback_data=f"challenge_difficulty_{key}"))
    buttons.append(difficulty_row)

    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_settings")])

    text = (
        "🧮 چالش‌های سرگرمی (سوالِ ریاضی)\n\n"
        f"فاصله‌ی فعلی بینِ چالش‌ها: {current_interval}\n"
        f"درجه‌سختیِ فعلی: {CHALLENGE_DIFFICULTY_LABELS[current_difficulty]}\n"
        "هر چالش تا ۱۲ ساعت وقت داره جواب داده بشه؛ اگه کسی جواب نده، هر ۱ ساعت یه‌بار دوباره "
        "بالای گپ میاد که یادِ همه بمونه.\n\n"
        "🟢 عادی: جمع/تفریق/ضرب/تقسیمِ ساده\n"
        "🟠 سخت: اعدادِ خیلی بزرگ‌تر + رادیکال\n"
        "🔴 وحشتناک: رادیکال، انتگرال، کسینوس، فیثاغورث و فیزیک"
    )
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def set_toggle_challenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    new_val = 0 if s["challenges_enabled"] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET challenges_enabled=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    await challenge_menu(update, context)


async def challenge_interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    seconds = int(update.callback_query.data.split("_")[-1])
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET challenge_interval_seconds=? WHERE chat_id=?", (seconds, chat_id))
    conn.commit()
    conn.close()
    await challenge_menu(update, context)


async def challenge_difficulty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    difficulty = update.callback_query.data[len("challenge_difficulty_"):]
    if difficulty not in CHALLENGE_DIFFICULTIES:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET challenge_difficulty=? WHERE chat_id=?", (difficulty, chat_id))
    conn.commit()
    conn.close()
    await challenge_menu(update, context)


async def set_toggle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_setting_and_refresh(update, context, "forward_allowed")


async def set_toggle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_setting_and_refresh(update, context, "allow_photo")


async def set_toggle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_setting_and_refresh(update, context, "allow_video")


async def set_toggle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_setting_and_refresh(update, context, "allow_sticker_gif")


async def set_toggle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_setting_and_refresh(update, context, "block_links")


async def set_toggle_owner_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _toggle_setting_and_refresh(update, context, "admin_actions_on_owner")


# ---- زیرمنوی پیام خوش‌آمد: روشن/خاموش + تنظیم عکس/گیفِ خوش‌آمدگویی ----------
async def welcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    status = "✅ روشن" if s["welcome_messages_enabled"] else "❌ خاموش"
    media_status = "🖼 عکس ست شده" if s["welcome_media_type"] == "photo" else ("🎞 گیف ست شده" if s["welcome_media_type"] == "animation" else "تنظیم نشده (فقط متن)")
    buttons = [
        [InlineKeyboardButton(f"وضعیت: {status}", callback_data="set_toggle_welcome")],
        [InlineKeyboardButton(f"🖼 تنظیم عکس/گیفِ خوش‌آمد ({media_status})", callback_data="welcome_media_start")],
    ]
    if s["welcome_media_type"]:
        buttons.append([InlineKeyboardButton("🗑 حذف عکس/گیفِ فعلی", callback_data="welcome_media_clear")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_settings")])
    await update.callback_query.edit_message_text(
        "پیام خوش‌آمد/خداحافظ:\nمی‌تونی روشن/خاموشش کنی و اگه بخوای، یه عکس یا گیف هم براش ست کنی "
        "(اون‌وقت متنِ خوش‌آمد به‌جای پیامِ ساده، کپشنِ همون عکس/گیف می‌شه).",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def set_toggle_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    new_val = 0 if s["welcome_messages_enabled"] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET welcome_messages_enabled=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    await welcome_menu(update, context)


async def welcome_media_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text(
        "یه عکس یا گیف (انیمیشن) بفرست که از این به بعد برای خوش‌آمدگویی استفاده بشه.\n"
        "برای انصراف /cancel رو بزن."
    )
    return WELCOME_MEDIA_WAIT


async def welcome_media_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = update.effective_chat.id
    if msg.photo:
        file_id = msg.photo[-1].file_id
        media_type = "photo"
    elif msg.animation:
        file_id = msg.animation.file_id
        media_type = "animation"
    else:
        await msg.reply_text("این یه عکس یا گیف نیست داداش؛ یه عکس یا گیف بفرست، یا /cancel بزن.")
        return WELCOME_MEDIA_WAIT
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE group_settings SET welcome_media_file_id=?, welcome_media_type=? WHERE chat_id=?",
        (file_id, media_type, chat_id),
    )
    conn.commit()
    conn.close()
    await msg.reply_text("✅ ثبت شد. از این به بعد این عکس/گیف با متنِ خوش‌آمد به‌عنوان کپشن فرستاده می‌شه.")
    # تا هندلرِ عمومیِ رسانه‌ی گروه (که پایین‌تر ثبت شده) رو همین پیام دوباره پردازش نکنه و چیز اضافه‌ای نگه
    raise ApplicationHandlerStop(ConversationHandler.END)


async def welcome_media_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET welcome_media_file_id=NULL, welcome_media_type=NULL WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    await update.callback_query.answer("پاک شد، برمی‌گردیم به پیام متنی ساده.")
    await welcome_menu(update, context)


# ---- زیرمنوی اسپم: روشن/خاموش + تنظیمات (چند پیام پشت سرهم / چند دقیقه سکوت) ----
async def set_spam_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    status = "✅ روشن" if s["spam_protection"] else "❌ خاموش"
    warn_status = "✅ فعال" if s["spam_warn_enabled"] else "❌ غیرفعال"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"وضعیت: {status}", callback_data="set_toggle_spam")],
            [InlineKeyboardButton(
                f"⚙️ تنظیمات (فعلا: {s['spam_threshold']} پیام ← {s['spam_mute_minutes']} دقیقه سکوت)",
                callback_data="set_spam_config_start",
            )],
            [InlineKeyboardButton(f"⚠️ اخطار موقع اسپم: {warn_status}", callback_data="set_toggle_spam_warn")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_settings")],
        ]
    )
    await update.callback_query.edit_message_text(
        "حالتت رو انتخاب کن:\n(اگه «اخطار موقع اسپم» فعال باشه، علاوه بر سکوت، یه اخطارِ واقعی هم می‌گیره)",
        reply_markup=keyboard,
    )


async def set_toggle_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    new_val = 0 if s["spam_protection"] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET spam_protection=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    await set_spam_menu(update, context)


async def set_toggle_spam_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    new_val = 0 if s["spam_warn_enabled"] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET spam_warn_enabled=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    await set_spam_menu(update, context)


async def set_spam_config_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text(
        "چند تا پیام پشت سر هم از یه کاربر ارسال بشه تا براش سکوت بزنم؟ یه عدد بفرست."
    )
    return SPAM_CFG_WAIT_THRESHOLD


async def set_spam_config_got_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if n < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("لطفا یه عدد صحیح بزرگ‌تر از صفر بفرست.")
        return SPAM_CFG_WAIT_THRESHOLD
    context.user_data["spam_cfg_threshold"] = n
    await update.message.reply_text(f"باشه. حالا چند دقیقه سکوت بدم؟ (حداکثر {MAX_SPAM_MUTE_MINUTES} دقیقه)")
    return SPAM_CFG_WAIT_MUTE


async def set_spam_config_got_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        minutes = int(update.message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("لطفا یه عدد صحیح بزرگ‌تر از صفر بفرست.")
        return SPAM_CFG_WAIT_MUTE
    minutes = min(minutes, MAX_SPAM_MUTE_MINUTES)
    threshold = context.user_data.pop("spam_cfg_threshold", 5)
    chat_id = update.effective_chat.id
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE group_settings SET spam_threshold=?, spam_mute_minutes=? WHERE chat_id=?",
        (threshold, minutes, chat_id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ تنظیم شد: بعد از {threshold} پیام پشت‌سرهم از یه کاربر، {minutes} دقیقه سکوت می‌شه."
    )
    return ConversationHandler.END


# ---- زیرمنوی یادآوری غیبت اعضا: هر چند ساعت یک‌بار یکی رو تصادفی تگ می‌کنه ----
async def eng_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    status = "✅ روشن" if s["engagement_enabled"] else "❌ خاموش"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"وضعیت: {status}", callback_data="eng_toggle")],
            [InlineKeyboardButton(
                f"⚙️ فاصله زمانی (فعلا هر {s['engagement_interval_hours']} ساعت)", callback_data="eng_interval_start"
            )],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_settings")],
        ]
    )
    await update.callback_query.edit_message_text(
        "هر چند وقت یک‌بار یه عضو رو تصادفی تگ می‌کنم و یه جمله‌ی باحال بهش می‌گم تا گروه رو زنده نگه داره:",
        reply_markup=keyboard,
    )


async def eng_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    new_val = 0 if s["engagement_enabled"] else 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET engagement_enabled=? WHERE chat_id=?", (new_val, chat_id))
    conn.commit()
    conn.close()
    await eng_menu(update, context)


async def eng_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return ConversationHandler.END
    await update.callback_query.edit_message_text("هر چند ساعت یک‌بار این پیام‌ها رو بفرستم؟ یه عدد بفرست.")
    return ENGAGEMENT_WAIT_HOURS


async def eng_interval_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = int(update.message.text.strip())
        if hours < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("لطفا یه عدد صحیح بزرگ‌تر از صفر بفرست.")
        return ENGAGEMENT_WAIT_HOURS
    chat_id = update.effective_chat.id
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE group_settings SET engagement_interval_hours=? WHERE chat_id=?", (hours, chat_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ تنظیم شد: هر {hours} ساعت یک‌بار.")
    return ConversationHandler.END


# ---- اعضا (لیستِ کامل: اسم/تاریخ عضویت/لقب/تعداد اخطار) + لینک دعوت --------------
async def adm_member_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    try:
        count = await context.bot.get_chat_member_count(chat_id)
    except Exception as e:
        await update.callback_query.edit_message_text(f"نشد تعداد اعضا رو بگیرم: {e}", reply_markup=back_to_admin_keyboard())
        return

    members = get_group_members(chat_id)  # فقط کسایی که ربات می‌شناستشون (محدودیتِ خودِ تلگرام؛ لیستِ کامل رو هیچ رباتی نمی‌گیره)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, first_seen FROM group_members WHERE chat_id=? AND is_present=1", (chat_id,))
    first_seen_map = {r["user_id"]: r["first_seen"] for r in c.fetchall()}
    c.execute("SELECT user_id, count FROM warnings WHERE chat_id=?", (chat_id,))
    warn_map = {r["user_id"]: r["count"] for r in c.fetchall()}
    c.execute("SELECT user_id, nickname FROM nicknames WHERE chat_id=?", (chat_id,))
    nick_map = {r["user_id"]: r["nickname"] for r in c.fetchall()}
    conn.close()

    lines = [f"👥 تعداد کلِ اعضای گروه: {count}", f"(از این تعداد، {len(members)} نفر رو می‌شناسم - چون تلگرام لیستِ کاملِ اعضا رو به هیچ رباتی نمی‌ده، فقط کسایی که پیام دادن قابل‌مشاهده‌ان)", ""]
    shown = 0
    for m in members[:40]:
        uid = m["user_id"]
        name = m["first_name"] or (f"@{m['username']}" if m["username"] else str(uid))
        fs = first_seen_map.get(uid)
        if fs:
            try:
                join_date, join_time = format_persian_date_time(datetime.fromisoformat(fs))
                join_str = f"{join_date} - {join_time}"
            except Exception:
                join_str = "-"
        else:
            join_str = "-"
        title = nick_map.get(uid)
        warn_count = warn_map.get(uid, 0)
        line = f"• {name} | عضویت: {join_str}"
        if title:
            line += f" | لقب: {title}"
        if warn_count:
            line += f" | ⚠️ {warn_count} اخطار"
        lines.append(line)
        shown += 1
    if len(members) > 40:
        lines.append(f"\n... و {len(members) - 40} نفرِ دیگه.")

    await update.callback_query.edit_message_text("\n".join(lines), reply_markup=back_to_admin_keyboard())


async def adm_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    try:
        link = await context.bot.export_chat_invite_link(chat_id)
        await update.callback_query.edit_message_text(
            f"🔗 لینک دعوت گروه:\n{link}", reply_markup=back_to_admin_keyboard()
        )
    except Exception as e:
        await update.callback_query.edit_message_text(
            f"نشد لینک دعوت رو بگیرم (باید ربات دسترسی 'دعوت با لینک' داشته باشه): {e}",
            reply_markup=back_to_admin_keyboard(),
        )


# ---- کلمه‌های آماده (چند لحن که هم‌زمان قابل فعال‌سازی‌ان، از گیت‌هاب خونده می‌شن) ------------
async def adm_ready_words_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    active = set(get_active_packs(chat_id))
    buttons = []
    for name, info in PACK_DEFINITIONS.items():
        configured = bool(pack_url(info))
        status = "✅" if name in active else "❌"
        note = "" if configured else " (تنظیم نشده)"
        buttons.append([InlineKeyboardButton(f"{status} {info['label']}{note}", callback_data=f"pack_toggle_{name}")])
    buttons.append([InlineKeyboardButton("🔄 بروزرسانی همه‌ی پک‌ها", callback_data="refresh_all_packs")])
    buttons.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm_back")])
    await update.callback_query.edit_message_text(
        "لحن خود را انتخاب کن (می‌تونی چندتا رو هم‌زمان روشن کنی، مثلاً هم عصبانی هم ناراحت):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def pack_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    chat_id = update.effective_chat.id
    pack_name = update.callback_query.data[len("pack_toggle_"):]
    info = PACK_DEFINITIONS.get(pack_name)
    if info is None:
        return
    if not pack_url(info):
        await update.callback_query.edit_message_text(
            f"لحن «{info['label']}» هنوز آماده نیست: اول باید لینک فایلش رو تو PACK_DEFINITIONS (کلید \"url\") "
            f"یا تو متغیر محیطی {info['url_env']} بذاری.",
            reply_markup=back_to_admin_keyboard(),
        )
        return
    now_active = toggle_active_pack(chat_id, pack_name)
    if now_active:
        await get_pack(pack_name)  # کش رو گرم می‌کنیم
    await adm_ready_words_menu(update, context)


async def refresh_all_packs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard_admin_callback(update, context):
        return
    total_words = 0
    refreshed = 0
    for name, info in PACK_DEFINITIONS.items():
        if not pack_url(info):
            continue
        pack = await get_pack(name, force_refresh=True)
        total_words += len(pack)
        refreshed += 1
    await update.callback_query.edit_message_text(
        f"✅ {refreshed} تا لحن بروزرسانی شد (مجموعاً {total_words} کلمه).",
        reply_markup=back_to_admin_keyboard(),
    )


# ---------------------------------------------------------------------------
# هندلر عمومی پیام‌های گروه: چک کلمات ممنوعه، جواب کلمات یاد گرفته شده، ردیابی پیام، فوروارد
# ---------------------------------------------------------------------------
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or msg.text is None:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    chat_id = chat.id
    user = update.effective_user

    # این کاربر رو به عنوان کسی که تو این گروه فعاله ثبت می‌کنیم (برای اینکه بشه تو پیوی هم روی این گروه کار کرد)
    record_user_group(user.id, chat_id, chat.title)
    upsert_group_member(chat_id, user)  # برای قابلیت تگ همگانی

    # ردیابی آی‌دی پیام برای قابلیت حذف پیام‌ها
    track_message(chat_id, msg.message_id)

    text_stripped = msg.text.strip()

    # ---- دستورهای سریع بدون نیاز به ریپلای ----

    # لینک گروه - فقط مالک - "لینک"
    if text_stripped == "لینک" and await is_user_owner(context, chat_id, user.id):
        try:
            link = await context.bot.export_chat_invite_link(chat_id)
            await reply_tracked(msg, f"🔗 بیا داداش:\n{link}")
        except Exception as e:
            await reply_tracked(msg, f"لینک رو نتونستم بگیرم: {e}")
        return

    # حافظه کولیبا - "یادت باشه <متن>" (خودِ فرد درباره‌ی خودش، برای همه آزاده)
    if text_stripped.startswith("یادت باشه "):
        note_text = text_stripped[len("یادت باشه "):].strip()
        if note_text:
            add_memory_note(chat_id, user.id, user.first_name or "کاربر", note_text)
            await reply_tracked(msg, f"✅ چشم، یادم موند: {note_text}")
        return

    # حافظه کولیبا - "چی یادته" (بدون ریپلای: یادداشت‌های خودت / با ریپلای: یادداشت‌های اون فرد)
    if text_stripped == "چی یادته":
        target_id, target_name = (
            (msg.reply_to_message.from_user.id, msg.reply_to_message.from_user.first_name)
            if msg.reply_to_message and msg.reply_to_message.from_user
            else (user.id, user.first_name)
        )
        notes = get_notes_for_user(chat_id, target_id)
        if not notes:
            await reply_tracked(msg, f"هنوز درباره‌ی {target_name or 'این یارو'} چیزی یادم نیست داداش.")
        else:
            lines = [f"🧠 اینا رو درباره‌ی {target_name or 'این یارو'} یادمه:"]
            lines += [f"- {n}" for n in notes]
            await reply_tracked(msg, "\n".join(lines))
        return

    # تگ همگانی - فقط مالک، باید ریپلای روی یه پیام باشه - "تگ همگانی"
    if text_stripped == "تگ همگانی" and await is_user_owner(context, chat_id, user.id):
        if not msg.reply_to_message:
            await reply_tracked(msg, "برای تگ همگانی اول رو یه پیام ریپلای بزن داداش.")
            return
        members = get_group_members(chat_id)
        if not members:
            await reply_tracked(
                msg,
                "هنوز کسی رو نمی‌شناسم؛ باید بچه‌ها حداقل یه پیام تو گروه داده باشن تا بشناسمشون.",
            )
            return
        target_message_id = msg.reply_to_message.message_id
        # هر مهرهٔ نامرئی (یه لینکِ تگ با متنِ کاراکتر با پهنای صفر) هنوز نوتیفیکیشنِ واقعی
        # برای همون کاربر می‌فرسته، ولی هیچ اسم/آی‌دی‌ای تو پیام دیده نمی‌شه.
        CHUNK = 50
        for i in range(0, len(members), CHUNK):
            chunk = members[i : i + CHUNK]
            invisible_pings = "".join(f'<a href="tg://user?id={m["user_id"]}">&#8203;</a>' for m in chunk)
            visible_text = "🔕 صدا زدم همه رو." if i == 0 else "\u200b"
            try:
                sent = await context.bot.send_message(
                    chat_id, visible_text + invisible_pings, parse_mode=ParseMode.HTML, reply_to_message_id=target_message_id
                )
                track_message(chat_id, sent.message_id)
            except Exception:
                pass
        return

    # "آمار" - آمارِ پیام‌های گروه + پرحرف‌ترین‌ها
    if text_stripped == "آمار":
        totals, top3 = get_group_message_stats(chat_id)
        total_all = totals["t"] + totals["p"] + totals["v"] + totals["s"] + totals["g"] + totals["o"]
        lines = [
            "📊 آمارِ این گروه:",
            f"کل پیام‌ها: {total_all}",
            f"🖼 عکس: {totals['p']}",
            f"🎬 ویدیو: {totals['v']}",
            f"🎭 استیکر: {totals['s']}",
            f"🎞 گیف: {totals['g']}",
        ]
        if top3:
            lines.append("")
            lines.append("🏆 پرحرف‌ترین‌های گروه:")
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(top3):
                lines.append(f"{medals[i]} {row['user_name']}: {row['total']} پیام")
        await reply_tracked(msg, "\n".join(lines))
        return

    # "حساب <عبارت>" - ماشین‌حسابِ ساده (جمع/تفریق/ضرب/تقسیم/توان/پرانتز)
    if text_stripped.startswith("حساب "):
        expr = text_stripped[len("حساب "):].strip()
        ok, result = calculate_expression(expr)
        if ok:
            await reply_tracked(msg, f"🧮 {format_calc_result(result)}")
        else:
            await reply_tracked(msg, "این یکی رو بلد نیستم داداش 🤷")
        return

    # "بگو" تنها (بدون متن بعدش) ریپلای‌شده روی یه عکس/گیف/ویدیو - چون خیلی از کلاینت‌های
    # تلگرام برای گیف اصلاً امکانِ گذاشتنِ کپشن رو نمی‌دن، این راهِ اصلیه برای گیف‌ها
    if text_stripped == "بگو" and msg.reply_to_message and (
        msg.reply_to_message.photo or msg.reply_to_message.animation or msg.reply_to_message.video
    ):
        media_msg = msg.reply_to_message
        try:
            if media_msg.photo:
                sent = await context.bot.send_photo(chat_id, media_msg.photo[-1].file_id)
            elif media_msg.animation:
                sent = await context.bot.send_animation(chat_id, media_msg.animation.file_id)
            else:
                sent = await context.bot.send_video(chat_id, media_msg.video.file_id)
            track_message(chat_id, sent.message_id)
        except Exception:
            pass
        return

    # "بگو <متن>" - ربات همون متن رو (بدون خودِ کلمه‌ی «بگو») می‌فرسته؛ اگه خودِ پیامِ
    # «بگو ...» ریپلایِ یه پیامِ دیگه بود، پیامِ ربات هم روی همون پیام ریپلای می‌شه
    if text_stripped.startswith("بگو "):
        say_text = text_stripped[len("بگو "):].strip()
        if say_text:
            reply_target = msg.reply_to_message.message_id if msg.reply_to_message else None
            sent = await context.bot.send_message(chat_id, say_text, reply_to_message_id=reply_target)
            track_message(chat_id, sent.message_id)
        return

    # "چت <متن>" - پیامِ خودِ فرستنده کاملاً حذف می‌شه و متن به‌جاش از طرفِ ربات ارسال می‌شه
    # (فقط ادمین). حذف رو همزمان با چک‌کردنِ ادمین بودن شروع می‌کنیم (نه بعدش) تا کمترین
    # تاخیرِ ممکن رو داشته باشه - نکته‌ی مهم: چون پیام همون لحظه‌ی ارسال تو کلاینتِ خودِ
    # فرستنده و بقیه دیده می‌شه، امکان نداره صد-در-صد و بدون هیچ ومضی دیده نشه (این یه
    # محدودیتِ خودِ تلگرامه، نه ربات)، ولی این‌جوری تقریباً بلافاصله پاک می‌شه.
    if text_stripped.startswith("چت "):
        delete_task = asyncio.create_task(_safe_delete_message(context, chat_id, msg.message_id))
        is_admin = await is_user_admin(update, context, user.id, chat_id)
        if not is_admin:
            await delete_task
            return
        say_text = text_stripped[len("چت "):].strip()
        if not say_text:
            await delete_task
            return
        reply_target = msg.reply_to_message.message_id if msg.reply_to_message else None
        await delete_task
        sent = await context.bot.send_message(chat_id, say_text, reply_to_message_id=reply_target)
        track_message(chat_id, sent.message_id)
        return

    # ---- دستورهای سریع ادمین (فقط وقتی ریپلای روی پیام یه عضو باشه و فرستنده ادمین باشه) ----
    if msg.reply_to_message and await is_user_admin(update, context, user.id, chat_id):
        target = msg.reply_to_message.from_user
        if target is None:
            # پیام ریپلای‌شده از طرف یه ادمین ناشناس یا کانال بوده، نمی‌تونیم کاربر مشخصی رو هدف بگیریم
            if text_stripped in ("بن", "صیکتیر", "اخطار", "آزاد") or text_stripped.startswith("اخطار ") or text_stripped == "سکوت" or re.match(r"^سکوت\s+\d+$", text_stripped):
                await reply_tracked(msg, "نفهمیدم این پیام از طرفِ کیه داداش (احتمالاً ادمینِ ناشناس یا خودِ کانال بوده).")
                return
            target = None  # بذار بقیه‌ی دستورها (مثل پین/آنپین که نیازی به target ندارن) ادامه پیدا کنن

        # اگه یکی روی خودِ پیام‌های ربات ریپلای بزنه و بخواد "بن/صیکتیر/سکوت/خفه/اخطار" کنه،
        # به‌جای اجرا (که اصلاً معنی نداره)، یه جواب بامزه می‌ده
        if target is not None and target.id == context.bot.id and _is_admin_command_word(text_stripped):
            await reply_tracked(msg, random.choice(BOT_SELF_TARGET_REPLIES))
            return

        # یادداشت ادمین درباره‌ی یه عضو دیگه - "یادداشت <متن>"
        if target is not None and text_stripped.startswith("یادداشت "):
            note_text = text_stripped[len("یادداشت "):].strip()
            if note_text:
                add_memory_note(chat_id, target.id, target.first_name or "کاربر", note_text)
                await reply_tracked(msg, f"✅ نوشتم داداش، یادم می‌مونه: {note_text}")
            return

        # بن سریع - "بن" یا "صیکتیر"
        if target is not None and text_stripped in ("بن", "صیکتیر"):
            if await is_owner_protected(context, chat_id, target.id):
                await reply_tracked(msg, OWNER_PROTECTED_TEXT.format(name=target.first_name))
                return
            try:
                await context.bot.ban_chat_member(chat_id, target.id)
                remove_mute_record(chat_id, target.id)
                record_ban(chat_id, target.id, target.first_name or "کاربر", "بن دستی")
                log_mod_action(chat_id, user.id, user.first_name or "ادمین", "ban", target.id, target.first_name or "")
                await reply_tracked(msg, f"🚫 {display_name(chat_id, target)} رفت بیرون، خداحافظ.")
            except Exception as e:
                await reply_tracked(msg, explain_admin_action_error(target.first_name, e))
            return

        # اخطار سریع - "اخطار" یا "اخطار <دلیل>"
        if target is not None and (text_stripped == "اخطار" or text_stripped.startswith("اخطار ")):
            if await is_owner_protected(context, chat_id, target.id):
                await reply_tracked(msg, OWNER_PROTECTED_TEXT.format(name=target.first_name))
                return
            reason = text_stripped[len("اخطار "):].strip() if text_stripped.startswith("اخطار ") else "اخطار سریع"
            await perform_warn(context, chat_id, target, reason, reply_target_message=msg)
            return

        # سکوت سریع - "سکوت"/"خفه" (نامحدود) یا "سکوت <عدد>"/"خفه <عدد>" (موقت، به دقیقه)
        if target is not None and (text_stripped in ("سکوت", "خفه") or re.match(r"^(سکوت|خفه)\s+\d+$", text_stripped)):
            if await is_owner_protected(context, chat_id, target.id):
                await reply_tracked(msg, OWNER_PROTECTED_TEXT.format(name=target.first_name))
                return
            mute_match = re.match(r"^(?:سکوت|خفه)\s+(\d+)$", text_stripped)
            try:
                if mute_match:
                    minutes = int(mute_match.group(1))
                    until = int(datetime.utcnow().timestamp() + minutes * 60)
                    await context.bot.restrict_chat_member(
                        chat_id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
                    )
                    record_mute(chat_id, target.id, target.first_name or "کاربر", until, "سکوت سریع")
                    log_mod_action(chat_id, user.id, user.first_name or "ادمین", "mute", target.id, target.first_name or "", f"{minutes} دقیقه")
                    await reply_tracked(msg, f"🔇 {display_name(chat_id, target)} برو یه {minutes} دقیقه استراحت کن داداش.")
                else:
                    # بدون عدد = سکوت نامحدود (تا وقتی خودمون با «آزاد» بازش کنیم)
                    await context.bot.restrict_chat_member(
                        chat_id, target.id, permissions=ChatPermissions(can_send_messages=False)
                    )
                    record_mute(chat_id, target.id, target.first_name or "کاربر", None, "سکوت سریع")
                    log_mod_action(chat_id, user.id, user.first_name or "ادمین", "mute", target.id, target.first_name or "", "نامحدود")
                    await reply_tracked(msg, f"🔇 {display_name(chat_id, target)} سکوته تا یکی «آزاد» بزنه، فعلاً نچ.")
            except Exception as e:
                await reply_tracked(msg, explain_admin_action_error(target.first_name, e))
            return

        # آزاد کردن از سکوت - "آزاد"
        if target is not None and text_stripped == "آزاد":
            ok, err = await unmute_member(context, chat_id, target.id)
            if ok:
                remove_mute_record(chat_id, target.id)
                log_mod_action(chat_id, user.id, user.first_name or "ادمین", "unmute", target.id, target.first_name or "")
                await reply_tracked(msg, f"🔊 خب {display_name(chat_id, target)} آزادی، حرف بزن.")
            else:
                await reply_tracked(msg, explain_admin_action_error(target.first_name, err))
            return

        # دادن لقب سریع - "لقب <متن>" (فقط ادمین، با ریپلای روی پیام عضو)
        if target is not None and text_stripped.startswith("لقب "):
            title_text = text_stripped[len("لقب "):].strip()[:16]
            if not title_text:
                await reply_tracked(msg, "خب یه چیزی هم بعد از «لقب» بنویس داداش. مثلاً: لقب پادشاه")
                return
            set_nickname(chat_id, target.id, title_text, user.id)
            await reply_tracked(msg, f"🏷 از الان {target.first_name} می‌شه «{title_text}»، قبول افتاد.")
            return

        # حذف لقب سریع - "حذف لقب" (فقط ادمین، با ریپلای روی پیام عضو)
        if target is not None and text_stripped == "حذف لقب":
            remove_nickname(chat_id, target.id)
            await reply_tracked(msg, f"🏷 باشه، لقبِ {target.first_name} رو برداشتم.")
            return

        # عضو ویژه‌ی سریع - "کولیبا ویژه" (فقط ادمین، با ریپلای روی پیام عضو)
        if target is not None and text_stripped == "کولیبا ویژه":
            add_special_member(chat_id, target.id)
            await reply_tracked(
                msg,
                f"🌟 {display_name(chat_id, target)} از الان عضو ویژه‌ست و به‌طور کامل از محدودیت‌های خودکار معافه.\n"
                "از پنل ادمین (🌟 افراد ویژه) می‌تونی معافیتش رو دستی/جزئی هم بکنی.",
            )
            return

        # پین سریع - "پین"
        if text_stripped == "پین":
            try:
                await context.bot.pin_chat_message(chat_id, msg.reply_to_message.message_id)
                await reply_tracked(msg, "📌 چسبوندمش بالا.")
            except Exception as e:
                await reply_tracked(msg, f"پین نشد داداش: {e}")
            return

        # آنپین سریع - "آنپین"
        if text_stripped == "آنپین":
            try:
                await context.bot.unpin_chat_message(chat_id, msg.reply_to_message.message_id)
                await reply_tracked(msg, "📌 باشه، از بالا برداشتمش.")
            except Exception as e:
                await reply_tracked(msg, f"آنپین نشد داداش: {e}")
            return

    # تنظیم فوروارد: اگه فوروارد ممنوع باشه و پیام فوروارد شده باشه، حذف + اخطار
    settings = get_settings(chat_id)
    is_admin_user = await is_user_admin(update, context, user.id, chat_id)
    if msg.forward_origin is not None and not settings["forward_allowed"] and not is_admin_user and not is_special_exempt(chat_id, user.id, "forward"):
        try:
            await msg.delete()
        except Exception:
            pass
        await warn_and_maybe_ban(context, chat_id, user, "فوروارد پیام")
        return

    # لینک: اگه ارسال لینک ممنوع باشه، حذف + اخطار
    if settings["block_links"] and not is_admin_user and not is_special_exempt(chat_id, user.id, "links") and URL_PATTERN.search(msg.text):
        try:
            await msg.delete()
        except Exception:
            pass
        await warn_and_maybe_ban(context, chat_id, user, "ارسال لینک")
        return

    # محافظت اسپم: اگه فعال باشه و کاربر (غیر ادمین/غیر ویژه) تو یه بازه‌ی کوتاه (چند ثانیه)
    # چندتا پیام پشت‌سرهم بده، سکوتش می‌کنیم. این بازه‌ی زمانی مهمه: قبلاً پیام‌ها فقط شمرده
    # می‌شدن (حتی اگه دقیقه‌ها فاصله داشتن)، که باعث می‌شد آدم‌های معمولی هم اشتباهی اسپم
    # تشخیص داده بشن. الان فقط اگه واقعاً «پشت سر هم و تند» باشن حساب می‌شه.
    real_owner_id = await get_group_owner_id(context, chat_id)
    is_owner_flag = real_owner_id is not None and real_owner_id == user.id
    # ادمین‌های معمولی همیشه از سیستم‌های خودکار (اسپم و ...) مستثنی می‌مونن. مالک هم به‌طور پیش‌فرض
    # مستثناست، مگر اینکه تنظیم «اعمال روی مالک» روشن باشه.
    spam_exempt = (
        is_special_exempt(chat_id, user.id, "spam")
        or (is_admin_user and not is_owner_flag)
        or (is_owner_flag and not settings["admin_actions_on_owner"])
    )
    if settings["spam_protection"] and not spam_exempt:
        spam_key = f"spam_{chat_id}"
        now_ts = datetime.utcnow().timestamp()
        state = context.chat_data.get(spam_key)
        if state is None or state["user_id"] != user.id or (now_ts - state["first_ts"]) > SPAM_WINDOW_SECONDS:
            state = {"user_id": user.id, "count": 1, "first_ts": now_ts}
        else:
            state["count"] += 1
        context.chat_data[spam_key] = state
        if state["count"] >= settings["spam_threshold"]:
            minutes = min(settings["spam_mute_minutes"], MAX_SPAM_MUTE_MINUTES)
            try:
                until = int(datetime.utcnow().timestamp() + minutes * 60)
                await context.bot.restrict_chat_member(
                    chat_id, user.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
                )
                record_mute(chat_id, user.id, user.first_name or "کاربر", until, "اسپم")
                log_mod_action(chat_id, 0, "سیستم ضداسپم", "mute", user.id, user.first_name or "", "اسپم خودکار")
                await send_tracked(
                    context.bot,
                    chat_id,
                    f"🔇 داداش {user.first_name} یه‌کم تند رفتی، برو {minutes} دقیقه یه نفس بکش.",
                )
            except Exception:
                pass
            if settings["spam_warn_enabled"]:
                await warn_and_maybe_ban(context, chat_id, user, "اسپم کردن")
            context.chat_data[spam_key] = {"user_id": None, "count": 0, "first_ts": 0}
            return

    # کلمات ممنوعه (اعضای ویژه‌ای که براش معاف تعریف شدن، معاف هستن)
    if not is_special_exempt(chat_id, user.id, "words"):
        bad_word = contains_forbidden_word(chat_id, msg.text)
        if bad_word:
            try:
                await msg.delete()
            except Exception:
                pass
            await warn_and_maybe_ban(context, chat_id, user, f"استفاده از کلمه ممنوعه ({bad_word})")
            return

    # جواب به کلمات یاد گرفته شده - اول لیستِ مالکِ واقعیِ گروه، بعد واکنش به اسم ربات، بعد پک‌های فعال
    owner_id = await get_group_owner_id(context, chat_id)
    answer = None
    if owner_id:
        answer = find_matching_word(chat_id, owner_id, msg.text)
    if not answer and BOT_NAME_COMMON_MISSPELLING in msg.text and BOT_NAME not in msg.text:
        answer = (
            f"آقا/خانم این اسمِ من نیست 😅 اسمِ من «{BOT_NAME}»ه، نه «{BOT_NAME_COMMON_MISSPELLING}».\n"
            f"برای اینکه جوابت رو بدم، باید دقیق بگی «{BOT_NAME}» 🌸"
        )
    elif not answer and BOT_NAME in msg.text:
        answer = random.choice(BOT_NAME_REACTIONS)
    if not answer:
        active_pack_names = get_active_packs(chat_id)
        if active_pack_names:
            packs_data = [await get_pack(name) for name in active_pack_names]
            answer = find_best_match_across_packs(packs_data, msg.text)
    if answer:
        await reply_tracked(msg, answer)


async def group_member_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """با ورود/خروج اعضا (از روی پیام سرویس)، لیست اعضا رو به‌روز نگه می‌داره و پیام خوش‌آمد/خداحافظ می‌فرسته.
    (chat_member_status_handler روش اصلی و مطمئن‌تره برای ردیابیِ خودِ عضویت؛ این علاوه بر اون، مسئول پیامه.)"""
    msg = update.message
    if msg is None:
        return
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "گروه"
    settings = get_settings(chat_id)
    welcome_on = bool(settings["welcome_messages_enabled"])

    if msg.new_chat_members:
        for member in msg.new_chat_members:
            upsert_group_member(chat_id, member)
            if member.is_bot or not welcome_on:
                continue
            date_str, time_str = format_persian_date_time()
            name_escaped = html.escape(member.first_name or "دوست")
            mention = f'<a href="tg://user?id={member.id}">{name_escaped}</a>'
            text = (
                f"به‌به {mention} اومد 🌸\n"
                f"خوش اومدی داداش/آبجی، بپیچ تو جمعِ {html.escape(chat_title)}!\n\n"
                f"تاریخ عضویت: {date_str}\n"
                f"ساعت عضویت: {time_str}"
            )
            try:
                media_type = settings["welcome_media_type"]
                media_id = settings["welcome_media_file_id"]
                if media_type == "photo" and media_id:
                    sent = await context.bot.send_photo(chat_id, media_id, caption=text, parse_mode=ParseMode.HTML)
                    track_message(chat_id, sent.message_id)
                elif media_type == "animation" and media_id:
                    sent = await context.bot.send_animation(chat_id, media_id, caption=text, parse_mode=ParseMode.HTML)
                    track_message(chat_id, sent.message_id)
                else:
                    await send_tracked(context.bot, chat_id, text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    if msg.left_chat_member:
        mark_member_left(chat_id, msg.left_chat_member.id)
        if not msg.left_chat_member.is_bot and welcome_on:
            date_str, time_str = format_persian_date_time()
            text = (
                f"😢 {msg.left_chat_member.first_name or 'یکی از بچه‌ها'} رفت از {chat_title}.\n"
                f"تاریخ خروج: {date_str}\n"
                f"ساعت خروج: {time_str}"
            )
            try:
                await send_tracked(context.bot, chat_id, text)
            except Exception:
                pass


async def chat_member_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    هر تغییر وضعیت عضویت (جوین/لفت/بن/ارتقا) رو از طریق chat_member آپدیت می‌گیره - این روش
    مستقل از پیام‌های سرویسِ «فلان کاربر اضافه شد» کار می‌کنه، پس قابل‌اعتمادتره.
    اگه کسی که قبلاً می‌شناختیم (رفته بود) دوباره برگرده، یه پیام خوشامد می‌فرستیم.

    نکته‌ی مهم: قبلاً این هندلر هیچ فیلترِ نوعِ چت نداشت، پس وقتی کانالِ اجباری اضافه شد و ربات
    توش عضو/ادمین شد، همینِ منطقِ «خوش برگشتی» برای عضوهای کانال هم اجرا می‌شد و ربات تو کانال
    پیام می‌فرستاد. الان اول چک می‌کنیم چتِ موردنظر خودِ کانالِ اجباریه یا نه (که فقط بحثِ
    بن‌کردنِ خودکار مطرحه، نه پیام دادن)، و بعدش هر چتِ غیرِ گروه/سوپرگروهِ دیگه رو کامل نادیده
    می‌گیریم.
    """
    cmu = update.chat_member
    if cmu is None:
        return
    chat_id = cmu.chat.id
    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    member_user = cmu.new_chat_member.user

    # کانالِ اجباری: فقط چک می‌کنیم این کاربر تو لیستِ بن‌شده‌های کانال هست یا نه؛ هیچ پیامی
    # نمی‌فرستیم، فقط اگه لازم بود بی‌سروصدا بیرونش می‌کنیم.
    if REQUIRED_CHANNEL_ID and str(chat_id) == str(REQUIRED_CHANNEL_ID):
        if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.RESTRICTED):
            if is_channel_banned(member_user.id):
                try:
                    await context.bot.ban_chat_member(chat_id, member_user.id)
                except Exception:
                    pass
        return

    if cmu.chat.type not in ("group", "supergroup"):
        return

    if new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        mark_member_left(chat_id, member_user.id)
        return

    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        prior = get_member_record(chat_id, member_user.id)
        was_gone = prior is not None and prior["is_present"] == 0
        upsert_group_member(chat_id, member_user)
        if was_gone and old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and not member_user.is_bot:
            try:
                phrase = random.choice(WELCOME_BACK_PHRASES)
                sent = await context.bot.send_message(chat_id, f"👋 {member_user.first_name} {phrase}")
                track_message(chat_id, sent.message_id)
            except Exception:
                pass


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    وقتی ربات به یه گروه اضافه می‌شه، فوراً لیست ادمین‌های اون لحظه رو ثبت می‌کنه (بیشترین کاری
    که تلگرام به یه ربات اجازه می‌ده - گرفتن لیست کامل اعضای عادی از قبل، از طریق Bot API ممکن نیست).
    """
    cmu = update.my_chat_member
    if cmu is None:
        return
    chat = cmu.chat
    if chat.type not in ("group", "supergroup"):
        return
    new_status = cmu.new_chat_member.status
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            for m in admins:
                upsert_group_member(chat.id, m.user)
        except Exception:
            pass


async def group_catch_all_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    از لحظه‌ای که ربات تو گروهه، هر پیامی (از هر نوعی - سند/صدا/لوکیشن/نظرسنجی/...، از هر کسی
    حتی ادمین‌ها و ربات‌های دیگه) رو برای قابلیت «حذف پیام‌ها» ردیابی می‌کنه. توجه: تلگرام به هیچ
    رباتی اجازه دسترسی به پیام‌های قبل از اضافه شدنش رو نمی‌ده؛ این فقط از همین لحظه به بعد کار می‌کنه.
    """
    msg = update.message
    if msg is None:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    track_message(chat.id, msg.message_id)
    if update.effective_user and not update.effective_user.is_bot:
        record_message_stat(chat.id, update.effective_user.id, update.effective_user.first_name, msg)


async def daily_bank_plan_job(context: ContextTypes.DEFAULT_TYPE):
    """هر شب دقیقاً نصفه‌شب (به وقت تهران) به کاربرایی که تو بانکِ پنالتی «طرح روزانه» رو فعال
    کردن (چون سکه‌شون از بانک وام گرفته بودن و بازم تموم شده بود)، سهمِ روزانه‌شون رو واریز می‌کنه.
    این کاملاً جدا از وام و برد/باختِ خودِ بازیه - فقط برای کسایی که این طرح رو دستی فعال کردن."""
    penalty_game.run_daily_plan_payouts()


async def engagement_job(context: ContextTypes.DEFAULT_TYPE):
    """هر بار که اجرا می‌شه، برای گروه‌هایی که این قابلیت روشنه و زمانش رسیده، یه عضو تصادفی رو تگ می‌کنه."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT chat_id, engagement_interval_hours, last_engagement_at FROM group_settings WHERE engagement_enabled=1"
    )
    rows = c.fetchall()
    conn.close()

    now = datetime.utcnow()
    for row in rows:
        chat_id = row["chat_id"]
        interval_hours = row["engagement_interval_hours"] or 6
        last_at = row["last_engagement_at"]
        due = True
        if last_at:
            try:
                due = (now - datetime.fromisoformat(last_at)).total_seconds() >= interval_hours * 3600
            except Exception:
                due = True
        if not due:
            continue

        members = get_group_members(chat_id)
        if not members:
            continue

        # ۴۰٪ مواقع (اگه یادداشتی موجود باشه)، به‌جای جمله‌ی عمومی، پیگیریِ یه یادداشتِ قبلی رو می‌پرسه
        note = get_random_note(chat_id) if random.random() < 0.4 else None
        if note:
            mention = f'<a href="tg://user?id={note["user_id"]}">{html.escape(note["user_name"] or "کاربر")}</a>'
            message_text = f"راستی {mention}، در مورد «{html.escape(note['note_text'])}» به کجا رسیدی؟ 😊"
        else:
            member = random.choice(members)
            phrase = random.choice(ENGAGEMENT_PHRASES)
            mention = f'<a href="tg://user?id={member["user_id"]}">{html.escape(member["first_name"] or "کاربر")}</a>'
            message_text = f"{mention} {phrase}"

        try:
            sent = await context.bot.send_message(chat_id, message_text, parse_mode=ParseMode.HTML)
            track_message(chat_id, sent.message_id)
        except Exception:
            continue

        conn2 = get_conn()
        c2 = conn2.cursor()
        c2.execute("UPDATE group_settings SET last_engagement_at=? WHERE chat_id=?", (now.isoformat(), chat_id))
        conn2.commit()
        conn2.close()


async def group_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """چک می‌کنه که آیا عکس/فیلم/استیکر/گیف تو این گروه مجازه یا نه (تنظیمات گروه)."""
    msg = update.message
    if msg is None:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    chat_id = chat.id
    user = update.effective_user
    upsert_group_member(chat_id, user)  # برای قابلیت تگ همگانی
    settings = get_settings(chat_id)
    is_admin_user = await is_user_admin(update, context, user.id, chat_id)

    blocked_reason = None
    if msg.photo and not settings["allow_photo"] and not is_admin_user and not is_special_exempt(chat_id, user.id, "photo"):
        blocked_reason = "ارسال عکس"
    elif msg.video and not settings["allow_video"] and not is_admin_user and not is_special_exempt(chat_id, user.id, "video"):
        blocked_reason = "ارسال فیلم"
    elif (msg.sticker or msg.animation) and not settings["allow_sticker_gif"] and not is_admin_user and not is_special_exempt(chat_id, user.id, "sticker"):
        blocked_reason = "ارسال استیکر/گیف"

    if blocked_reason:
        try:
            await msg.delete()
        except Exception:
            pass
        await warn_and_maybe_ban(context, chat_id, user, blocked_reason)
        return

    track_message(chat_id, msg.message_id)


async def non_admin_click_guard_no_op(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """برای دکمه‌هایی که فقط پیام اطلاع رسانی می‌دن (مثل زمانی که کاربر عادی روی دکمه ادمین بزنه)."""
    query = update.callback_query
    await query.answer("این گزینه فقط برای مدیر گروه است.", show_alert=True)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# راه‌اندازی برنامه و ثبت هندلرها
# ---------------------------------------------------------------------------
class _TrackingExtBot(ExtBot):
    """
    نسخه‌ی سفارشی‌شده‌ی ExtBot: بعد از فرستادنِ هر پیام (به یه گروه)، خودکار تو
    tracked_messages ثبتش می‌کنه - تا قابلیتِ «حذف پیام‌ها» پیام‌های خودِ ربات رو هم بتونه
    پاک کنه، حتی جاهایی از کد (تو bot.py یا تو dooz_game/rps_game/chess_game) که فراموش
    کردن صراحتاً track_message() صدا بزنن.

    نکته‌ی مهم: نسخه‌ی قبلیِ این قابلیت با setattr روی نمونه‌ی app.bot پیاده شده بود، ولی
    ExtBot از __slots__ استفاده می‌کنه و اصلاً اجازه‌ی ست‌کردنِ اتریبیوتِ جدید رو نمی‌ده -
    همون چیزی که باعثِ کرش‌کردنِ کاملِ ربات شد. زیرکلاس‌کردن (این‌جا) روشِ درست و رسمیِ
    خودِ python-telegram-bot برای این کاره.
    """

    async def _send_and_track(self, coro):
        result = await coro
        try:
            if result is not None:
                chat_id = getattr(result, "chat_id", None)
                message_id = getattr(result, "message_id", None)
                if chat_id is not None and message_id is not None and int(chat_id) < 0:
                    track_message(int(chat_id), message_id)
        except Exception:
            pass
        return result

    @staticmethod
    def _blocked_channel_target(args, kwargs) -> bool:
        """محافظِ سخت: کانالِ اجباری فقط برای چک‌کردنِ عضویته، نه برای پیام‌دادن. اینجا رو
        همه‌ی متدهای send_* می‌ذاریم تا هیچ بخشی از کد (الان یا تو آپدیت‌های بعدی) نتونه
        سهواً چیزی به این کانال بفرسته."""
        if not REQUIRED_CHANNEL_ID:
            return False
        target = kwargs.get("chat_id", args[0] if args else None)
        return target is not None and str(target) == str(REQUIRED_CHANNEL_ID)

    async def send_message(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_message(*args, **kwargs))

    async def send_photo(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_photo(*args, **kwargs))

    async def send_animation(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_animation(*args, **kwargs))

    async def send_video(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_video(*args, **kwargs))

    async def send_document(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_document(*args, **kwargs))

    async def send_voice(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_voice(*args, **kwargs))

    async def send_audio(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_audio(*args, **kwargs))

    async def send_sticker(self, *args, **kwargs):
        if self._blocked_channel_target(args, kwargs):
            return None
        return await self._send_and_track(super().send_sticker(*args, **kwargs))



def build_application() -> Application:
    try:
        custom_bot = _TrackingExtBot(token=BOT_TOKEN)
        app = Application.builder().bot(custom_bot).build()
    except Exception as e:
        logger.warning(f"⚠️ نتونستم بات با ردیابیِ خودکارِ پیام‌ها بسازم ({e})؛ بدونِ اون قابلیت ادامه می‌دم تا ربات لااقل بالا بیاد.")
        app = Application.builder().token(BOT_TOKEN).build()

    # این باید همیشه اولین چیزی باشه که برای هر آپدیت (هر نوعی، پیام یا کالبک) اجرا می‌شه، تا اگه
    # سوپرمالک ربات رو کلاً خاموش کرده، هیچ‌چیزِ دیگه‌ای (حتی خودِ پنل ادمین) اجرا نشه.
    app.add_handler(TypeHandler(Update, global_kill_switch_guard), group=-10)

    def _is_on_command(text: str, reply_to) -> bool:
        if text == "کولیبا روشن":
            return True
        if text == "روشن" and reply_to and reply_to.from_user and reply_to.from_user.is_bot:
            return True
        return False

    def _is_off_command(text: str, reply_to) -> bool:
        if text == "کولیبا خاموش":
            return True
        if text == "خاموش" and reply_to and reply_to.from_user and reply_to.from_user.is_bot:
            return True
        return False

    async def group_pause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        هم گیت‌ه (اگه ربات برای این گروه خاموشه، جلوی بقیه‌ی هندلرها رو می‌گیره) و هم خودِ
        دستورِ خاموش/روشن‌کردن رو اجرا می‌کنه. هر دو تو یه هندلرِ واحدن چون تلگرام برای هر
        پیام فقط یه هندلر رو تو هر گروه اجرا می‌کنه - اگه جداشون می‌کردیم، فیلترِ گسترده‌ی
        گیت همیشه اول می‌گرفت و اون یکی هیچ‌وقت اجرا نمی‌شد.
        """
        msg = update.effective_message
        chat = update.effective_chat
        if not msg or not chat or chat.type not in ("group", "supergroup") or not msg.text:
            return
        text_stripped = msg.text.strip()
        user = update.effective_user

        is_on = _is_on_command(text_stripped, msg.reply_to_message)
        is_off = _is_off_command(text_stripped, msg.reply_to_message)

        if is_on or is_off:
            if not await is_user_admin(update, context, user.id, chat.id):
                await reply_tracked(msg, "⛔ خاموش‌روشن‌کردنِ من فقط دستِ ادمیناست داداش.")
                raise ApplicationHandlerStop
            try:
                set_bot_paused(chat.id, is_off)
            except Exception as e:
                logger.warning(f"⚠️ ست‌کردنِ bot_paused برای چت {chat.id} شکست خورد: {e}")
                await reply_tracked(msg, "یه مشکلِ فنی پیش اومد داداش، دوباره امتحان کن.")
                raise ApplicationHandlerStop
            if is_off:
                await reply_tracked(msg, "🌙 باشه داداش، ساکت می‌شم تا صدام کنی. برای برگردوندنم بگو «روشن» یا «کولیبا روشن».")
            else:
                await reply_tracked(msg, "😄 برگشتم! چه خبرا داداش؟")
            raise ApplicationHandlerStop

        settings = get_settings(chat.id)
        if row_get(settings, "bot_paused", 0):
            raise ApplicationHandlerStop

    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, group_pause_handler), group=-1)

    # /start
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("version", version_command))
    app.add_handler(MessageHandler(filters.Regex(r"^نسخه$"), version_command))
    app.add_handler(CallbackQueryHandler(check_channel_join_callback, pattern="^check_channel_join$"))
    app.add_handler(CallbackQueryHandler(start_help_callback, pattern="^start_help$"))
    app.add_handler(CallbackQueryHandler(start_back_callback, pattern="^start_back$"))
    # /admin - ورود به پنل ادمین داخل گروه
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(MessageHandler(filters.Regex(r"^پنل$") & filters.ChatType.GROUPS, admin_panel))

    # ---- «چت» از طریق پیوی -----------------------------------------------
    app.add_handler(MessageHandler(filters.Regex(r"^چت($|\s+.+)$") & filters.ChatType.PRIVATE, pv_chat_dispatch))

    # مکالمه: پیام همگانی (فقط سوپرادمین، فقط از پیوی)
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("broadcast", broadcast_start, filters=filters.ChatType.PRIVATE),
                MessageHandler(filters.Regex(r"^همگانی$") & filters.ChatType.PRIVATE, broadcast_start),
            ],
            states={BROADCAST_WAIT_CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_got_content)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )
    app.add_handler(
        MessageHandler((filters.PHOTO | filters.ANIMATION | filters.VIDEO) & filters.ChatType.PRIVATE, pv_media_with_caption)
    )

    # پنلِ مخصوصِ سوپرمالک (کاملاً مخفی؛ فقط با عبارتِ دقیقِ «پنل مخصوص» تو پیوی)
    app.add_handler(MessageHandler(filters.Regex(r"^پنل مخصوص$") & filters.ChatType.PRIVATE, super_panel_entry))
    app.add_handler(CallbackQueryHandler(superpanel_toggle_bot_callback, pattern="^superpanel_toggle_bot$"))
    app.add_handler(CallbackQueryHandler(superpanel_close_callback, pattern="^superpanel_close$"))
    app.add_handler(CallbackQueryHandler(superpanel_back_main_callback, pattern="^superpanel_back_main$"))
    app.add_handler(CallbackQueryHandler(superpanel_channel_ban_menu, pattern="^superpanel_channel_ban_menu$"))
    app.add_handler(CallbackQueryHandler(superpanel_channel_ban_list_callback, pattern="^superpanel_channel_ban_list$"))
    app.add_handler(CallbackQueryHandler(superpanel_channel_unban_callback, pattern="^superpanel_channel_unban_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(superpanel_mathstats_callback, pattern="^superpanel_mathstats_\\d+$"))
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(superpanel_xp_start, pattern="^superpanel_xp_start$")],
            states={
                SUPERPANEL_XP_WAIT_USER: [MessageHandler(filters.ALL & ~filters.COMMAND, superpanel_xp_got_user)],
                SUPERPANEL_XP_WAIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, superpanel_xp_got_amount)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(superpanel_channel_ban_start, pattern="^superpanel_channel_ban_start$")],
            states={
                SUPERPANEL_CHANNEL_BAN_WAIT_USER: [MessageHandler(filters.ALL & ~filters.COMMAND, superpanel_channel_ban_got_user)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    app.add_handler(MessageHandler(filters.Regex(r"^@") & filters.ChatType.PRIVATE, pv_awaiting_username))
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^گروه[\s‌]*های[\s‌]+عضو[\s‌]+شده$") & filters.ChatType.PRIVATE,
            super_owner_group_list,
        )
    )
    app.add_handler(CallbackQueryHandler(pv_link_pick_callback, pattern="^pvlink_"))
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^حساب(\s+.+)?$|^(?=.*[\+\-\*/x×÷\^%])[0-9۰-۹٠-٩\.\+\-\*/x×÷\^%()\s,،]+$")
            & filters.ChatType.PRIVATE,
            pv_calculator_handler,
        )
    )
    app.add_handler(
        MessageHandler((filters.PHOTO | filters.ANIMATION | filters.VIDEO) & filters.ChatType.GROUPS, group_media_say_handler)
    )
    app.add_handler(CommandHandler("cancel", cancel_conversation))

    # مکالمه: یاد دادن کلمه جدید (هم از دکمه شیشه‌ای، هم از دستور /addword که تو منوی ☰ ربات هست)
    learn_word_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(learn_word_start, pattern="^learn_word$"),
            CommandHandler("addword", learn_word_start),
        ],
        states={
            LEARN_WORD_WAIT_TRIGGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, learn_word_got_trigger)],
            LEARN_WORD_WAIT_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, learn_word_got_answer)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )
    app.add_handler(learn_word_conv)
    # /mywords - معادل دکمه «دیدن کلمات ساخته شده»، از منوی ☰ ربات در پیوی در دسترسه
    app.add_handler(CommandHandler("mywords", view_words))
    # انتخاب گروه هدف وقتی کاربر تو پیوی چند گروه داره
    app.add_handler(CallbackQueryHandler(select_group_callback, pattern="^selgrp_"))

    # مکالمه: ویرایش کلمه (تغییر جواب / اضافه کردن جواب رندوم)
    edit_word_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(word_action_change, pattern="^wact_change$"),
            CallbackQueryHandler(word_action_add_extra, pattern="^wact_add_extra$"),
        ],
        states={
            EDIT_WORD_WAIT_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, word_edit_got_answer)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )
    app.add_handler(edit_word_conv)

    # کالبک‌های مربوط به دیدن/ویرایش کلمات و منوی اصلی
    app.add_handler(CallbackQueryHandler(view_words, pattern="^view_words$"))
    app.add_handler(CallbackQueryHandler(word_edit_menu, pattern="^word_edit_\\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_main_menu, pattern="^back_main$"))

    # مکالمه: حذف کامل یه کلمه با کد
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(worddel_start, pattern="^worddel_start$")],
            states={DELETE_WORD_WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, worddel_got_code)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # پنل ادمین: منوی اصلی و بازگشت
    app.add_handler(CallbackQueryHandler(admin_back, pattern="^adm_back$"))
    app.add_handler(CallbackQueryHandler(adm_lock, pattern="^adm_lock$"))
    app.add_handler(CallbackQueryHandler(adm_unlock, pattern="^adm_unlock$"))
    app.add_handler(CallbackQueryHandler(adm_forbidden_menu, pattern="^adm_forbidden$"))
    app.add_handler(CallbackQueryHandler(adm_delete_msgs_menu, pattern="^adm_delete_msgs$"))
    app.add_handler(CallbackQueryHandler(adm_special_menu, pattern="^adm_special$"))
    app.add_handler(CallbackQueryHandler(special_manage_menu, pattern="^special_manage_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(special_mode_all_callback, pattern="^special_mode_all_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(special_mode_custom_callback, pattern="^special_mode_custom_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(special_toggle_feature_callback, pattern="^special_toggle_[a-z]+_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(special_remove_callback, pattern="^special_remove_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(adm_mutelist_menu, pattern="^adm_mutelist$"))
    app.add_handler(CallbackQueryHandler(adm_unmute_callback, pattern="^adm_unmute_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(adm_banlist_menu, pattern="^adm_banlist$"))
    app.add_handler(CallbackQueryHandler(adm_unban_callback, pattern="^adm_unban_\\-?\\d+$"))
    app.add_handler(CallbackQueryHandler(adm_unban_all_callback, pattern="^adm_unban_all$"))
    app.add_handler(CallbackQueryHandler(adm_settings_menu, pattern="^adm_settings$"))
    app.add_handler(CallbackQueryHandler(adm_member_count, pattern="^adm_member_count$"))
    app.add_handler(CallbackQueryHandler(adm_invite_link, pattern="^adm_invite_link$"))
    app.add_handler(CallbackQueryHandler(adm_ready_words_menu, pattern="^adm_ready_words$"))
    app.add_handler(CallbackQueryHandler(pack_toggle_callback, pattern="^pack_toggle_\\w+$"))
    app.add_handler(CallbackQueryHandler(refresh_all_packs, pattern="^refresh_all_packs$"))
    app.add_handler(CallbackQueryHandler(set_toggle_forward, pattern="^set_toggle_forward$"))
    app.add_handler(CallbackQueryHandler(set_toggle_photo, pattern="^set_toggle_photo$"))
    app.add_handler(CallbackQueryHandler(set_toggle_video, pattern="^set_toggle_video$"))
    app.add_handler(CallbackQueryHandler(set_toggle_sticker, pattern="^set_toggle_sticker$"))
    app.add_handler(CallbackQueryHandler(set_toggle_links, pattern="^set_toggle_links$"))
    app.add_handler(CallbackQueryHandler(welcome_menu, pattern="^welcome_menu$"))
    app.add_handler(CallbackQueryHandler(set_toggle_welcome, pattern="^set_toggle_welcome$"))
    app.add_handler(CallbackQueryHandler(welcome_media_clear, pattern="^welcome_media_clear$"))
    app.add_handler(CallbackQueryHandler(set_toggle_owner_actions, pattern="^set_toggle_owner_actions$"))
    app.add_handler(CallbackQueryHandler(challenge_menu, pattern="^challenge_menu$"))
    app.add_handler(CallbackQueryHandler(set_toggle_challenges, pattern="^set_toggle_challenges$"))
    app.add_handler(CallbackQueryHandler(challenge_interval_callback, pattern="^challenge_interval_\\d+$"))
    app.add_handler(CallbackQueryHandler(challenge_difficulty_callback, pattern="^challenge_difficulty_(normal|hard|terrifying)$"))
    app.add_handler(CallbackQueryHandler(set_spam_menu, pattern="^set_spam_menu$"))
    app.add_handler(CallbackQueryHandler(set_toggle_spam, pattern="^set_toggle_spam$"))
    app.add_handler(CallbackQueryHandler(set_toggle_spam_warn, pattern="^set_toggle_spam_warn$"))
    app.add_handler(CallbackQueryHandler(eng_menu, pattern="^eng_menu$"))
    app.add_handler(CallbackQueryHandler(eng_toggle, pattern="^eng_toggle$"))
    app.add_handler(CallbackQueryHandler(adm_close_panel, pattern="^adm_close_panel$"))
    app.add_handler(CallbackQueryHandler(del_all_callback, pattern="^del_all$"))
    app.add_handler(CallbackQueryHandler(warn_view_callback, pattern="^warnview_\\d+$"))
    app.add_handler(CallbackQueryHandler(warn_remove_callback, pattern="^warnremove_\\d+$"))

    # مکالمه: تنظیمات یادآوری غیبت اعضا (فاصله زمانی به ساعت)
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(eng_interval_start, pattern="^eng_interval_start$")],
            states={ENGAGEMENT_WAIT_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, eng_interval_got)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: تنظیمات اسپم (چند پیام پشت سرهم / چند دقیقه سکوت)
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(set_spam_config_start, pattern="^set_spam_config_start$")],
            states={
                SPAM_CFG_WAIT_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_spam_config_got_threshold)],
                SPAM_CFG_WAIT_MUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_spam_config_got_mute)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: تنظیم عکس/گیفِ پیام خوش‌آمد
    # نکته‌ی مهم: این باید با group=-1 ثبت بشه، وگرنه هندلرِ عمومیِ رسانه‌ی گروه (که پایین‌تر
    # تو group=0 ثبت می‌شه) زودتر از این پیام رو قاپ می‌زنه و اصلاً به این مکالمه نمی‌رسه.
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(welcome_media_start, pattern="^welcome_media_start$")],
            states={WELCOME_MEDIA_WAIT: [MessageHandler((filters.PHOTO | filters.ANIMATION) & ~filters.COMMAND, welcome_media_got)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        ),
        group=-1,
    )

    # مکالمه: دادن لقب
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(adm_title_start, pattern="^adm_title$")],
            states={ADD_TITLE_WAIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_title_got_text)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: ثبت اخطار
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(adm_warn_add_start, pattern="^adm_warn_add$")],
            states={WARN_WAIT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_warn_add_got_reason)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: حذف اخطار (از منوی ادمین)
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(adm_warn_remove_start, pattern="^adm_warn_remove$")],
            states={REMOVE_WARN_WAIT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_warn_remove_got_user)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: اضافه کردن کلمه ممنوعه
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(forb_add_start, pattern="^forb_add$")],
            states={FORBIDDEN_ADD_WAIT_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, forb_add_got_word)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: حذف کلمه ممنوعه
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(forb_remove_start, pattern="^forb_remove$")],
            states={FORBIDDEN_REMOVE_WAIT_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, forb_remove_got_word)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: حذف N پیام
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(del_n_start, pattern="^del_n$")],
            states={DELETE_N_WAIT_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, del_n_got_number)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # مکالمه: اضافه کردن عضو ویژه + لحن مخصوص
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(special_add_start, pattern="^special_add$")],
            states={SPECIAL_ADD_WAIT_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, special_add_got_user)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(special_tone_start, pattern="^special_tone_\\d+$")],
            states={SPECIAL_TONE_WAIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, special_tone_got_text)]},
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            per_message=False,
        )
    )

    # هندلر عمومی پیام‌های متنی گروه (باید بعد از همه ConversationHandler ها ثبت بشه)
    # این باید با group=-3 ثبت بشه، و مهم‌تر از اون، باید زودتر از challenge_time_check_handler
    # (group=-2) اجرا بشه: وگرنه اگه چالش یه‌کم قدیمی باشه، اون هندلرِ زمان‌بند زودتر میاد چالش رو
    # منقضی می‌کنه و یکی جدید جاش می‌ذاره، و وقتی نوبت به این هندلر می‌رسه دیگه چالشی که کاربر روش
    # ریپلای زده وجود نداره - نتیجه‌ش سکوتِ کاملِ ربات بود، نه تاییدِ درست/غلط.
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, challenge_answer_handler),
        group=-3,
    )
    # این هم باید تو گروهِ خودش باشه (نه همون -3)، چون فیلترش مشابهِ بالاست (ریپلای+متن)؛ اگه تو یه
    # گروه با challenge_answer_handler ثبت می‌شد، فقط یکیشون برای هر پیام اجرا می‌شد.
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, math_leaderboard_handler),
        group=-4,
    )
    # سوال‌های ویژه هم باید تو گروهِ خودشون باشن، چون فیلترشون هم مشابهِ بالاست.
    app.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, bonus_challenge_answer_handler),
        group=-5,
    )
    # مسیرِ پشتیبانِ زمان‌بندیِ چالش: با هر پیامِ گروه چک می‌کنه نوبتِ چالشِ بعدی رسیده یا نه (توضیح
    # کامل تو خودِ تابعِ maybe_post_challenge_for_chat). این باید قبل از هندلرِ عمومی اجرا بشه، ولی
    # چون توقفی تو انتشار نمی‌ندازه، بعدش هندلرِ عمومی هم طبقِ معمول برای همون پیام اجرا می‌شه.
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, challenge_time_check_handler), group=-2)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, group_message_handler))

    # هندلر رسانه‌های گروه: عکس/فیلم/استیکر/گیف (طبق تنظیمات گروه مجاز یا حذف می‌شن)
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.VIDEO | filters.Sticker.ALL | filters.ANIMATION) & filters.ChatType.GROUPS,
            group_media_handler,
        )
    )

    # هندلر ورود/خروج اعضا (برای به‌روز نگه‌داشتن لیستِ تگ همگانی)
    app.add_handler(
        MessageHandler(
            (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER) & filters.ChatType.GROUPS,
            group_member_update_handler,
        )
    )

    # هندلر chat_member (روش مطمئن‌تر برای ردیابی جوین/لفت/بن اعضا - برای تگ همگانی)
    app.add_handler(ChatMemberHandler(chat_member_status_handler, ChatMemberHandler.CHAT_MEMBER))

    # هندلر my_chat_member (وقتی ربات به یه گروه اضافه می‌شه، فوراً ادمین‌های اون گروه رو ثبت می‌کنه)
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    # ردیاب عمومی: هر نوع پیامی تو گروه (سند/صدا/لوکیشن/نظرسنجی/...) رو برای «حذف پیام‌ها» ثبت می‌کنه.
    # تو گروه جدا (group=1) ثبت شده تا مستقل از هندلرهای بالا (که ممکنه پیام رو حذف کرده باشن) اجرا بشه.
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, group_catch_all_tracker), group=1)

    # بازی دوز - ماژول کاملاً مستقل (dooz_game.py)؛ تو گروه ۲ ثبت می‌شه تا با هندلر عمومی متنی گروه تداخل نکنه
    dooz_game.register(app)
    # بازی سنگ‌کاغذقیچی - ماژول کاملاً مستقل (rps_game.py)
    rps_game.register(app)
    # بازی شطرنج - ماژول کاملاً مستقل (chess_game.py)
    chess_game.register(app)
    # بازی پنالتی - ماژول کاملاً مستقل (penalty_game.py)
    penalty_game.register(app)

    # ---- منوی مشترکِ «بازی» (انتخاب بین دوز، سنگ‌کاغذقیچی، شطرنج، و پنالتی) ------------
    def _games_picker_keyboard():
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🪨📄✂️ سنگ‌کاغذقیچی", callback_data="games_pick_rps")],
                [InlineKeyboardButton("⭕❌ دوز", callback_data="games_pick_dooz")],
                [InlineKeyboardButton("♟️ شطرنج", callback_data="games_pick_chess")],
                [InlineKeyboardButton("⚽ پنالتی", callback_data="games_pick_penalty")],
            ]
        )

    async def games_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("کدوم بازی رو می‌خوای؟", reply_markup=_games_picker_keyboard())

    async def games_pick_dooz_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("🎮 دوز! اول نوعش رو انتخاب کن:", reply_markup=dooz_game.type_menu_keyboard())

    async def games_pick_rps_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🪨📄✂️ سنگ‌کاغذقیچی! چطوری بازی کنیم؟", reply_markup=rps_game.mode_menu_keyboard()
        )

    async def games_pick_chess_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("♟️ شطرنج! چطوری بازی کنیم؟", reply_markup=chess_game.mode_menu_keyboard())

    async def games_pick_penalty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(penalty_game.welcome_text(), reply_markup=penalty_game.main_menu_keyboard())

    async def games_back_main_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("کدوم بازی رو می‌خوای؟", reply_markup=_games_picker_keyboard())

    async def games_challenge_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """وقتی رو پیامِ یکی دیگه ریپلای می‌زنن و می‌نویسن «بازی» - بر اساس آخرین انتخابِ
        کاربر از منو (دوز، سنگ‌کاغذقیچی یا شطرنج)، چالش رو تو همون بازی شروع می‌کنه."""
        kind, extra = game_router.pop_pending(update.effective_user.id)
        if kind == "rps":
            await rps_game.rps_challenge_trigger(update, context)
        elif kind == "chess":
            await chess_game.chess_challenge_trigger(update, context)
        elif kind == "penalty":
            await penalty_game.penalty_challenge_trigger(update, context, forced_rounds=extra)
        else:
            await dooz_game.dooz_challenge_trigger(update, context, forced_variant=extra or "limited")

    app.add_handler(MessageHandler(filters.Regex(r"^بازی$") & ~filters.REPLY & filters.ChatType.GROUPS, games_entry), group=2)
    app.add_handler(MessageHandler(filters.Regex(r"^بازی$") & filters.REPLY & filters.ChatType.GROUPS, games_challenge_dispatch), group=2)
    app.add_handler(CallbackQueryHandler(games_pick_dooz_cb, pattern="^games_pick_dooz$"))
    app.add_handler(CallbackQueryHandler(games_pick_rps_cb, pattern="^games_pick_rps$"))
    app.add_handler(CallbackQueryHandler(games_pick_chess_cb, pattern="^games_pick_chess$"))
    app.add_handler(CallbackQueryHandler(games_pick_penalty_cb, pattern="^games_pick_penalty$"))
    app.add_handler(CallbackQueryHandler(games_back_main_cb, pattern="^games_back_main$"))

    app.post_init = setup_bot_menu
    return app


async def setup_bot_menu(app: Application):
    """به جای دکمه‌های شیشه‌ای، تو پیوی از منوی رسمی (☰) خود ربات استفاده می‌کنیم.
    نکته‌ی مهم: اگه GAME_PANEL_URL ست شده باشه، دکمه‌ی منو رو به‌جای «Commands» می‌ذاریم رو
    «Web App» - این دقیقاً همون چیزیه که باعث می‌شه لینک‌های t.me/<bot>?startapp=<id>
    (که تو دعوت‌نامه‌های بازی استفاده می‌کنیم) مستقیم پنل رو با همون game_id باز کنن، نه یه
    پیامِ متنیِ ساده."""
    await app.bot.set_my_commands(
        [
            BotCommand("start", "شروع / منوی اصلی"),
            BotCommand("addword", "یاد دادن کلمه جدید"),
            BotCommand("mywords", "دیدن کلمات ساخته شده"),
            BotCommand("admin", "پنل مدیریت گروه (فقط داخل گروه)"),
            BotCommand("cancel", "لغو عملیات جاری"),
        ]
    )
    if GAME_PANEL_URL:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎮 بازی‌ها", web_app=WebAppInfo(url=GAME_PANEL_URL)))
    else:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def main():
    if BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        print("⚠️  لطفا اول توکن ربات رو در متغیر BOT_TOKEN یا متغیر محیطی BOT_TOKEN قرار بده.")
        return
    if not pg_compat.postgres_enabled() and os.environ.get("ALLOW_SQLITE_FALLBACK") != "1":
        print(
            "⛔ متغیرِ DATABASE_URL (یا DATABASE_PUBLIC_URL / POSTGRES_URL) پیدا نشد، پس ربات داشت "
            "می‌رفت رو یه فایلِ SQLiteِ محلی کار کنه - که رو Railway هر دیپلوی از نو خالی می‌شه.\n"
            "برای جلوگیری از این‌که بی‌خبر دیتا از دست بره، ربات عمداً بالا نیومد.\n\n"
            "→ اگه قراره رو Railway/Postgres اجرا بشه: مطمئن شو DATABASE_URL درست تنظیم شده "
            "(تو RAILWAY_POSTGRES_SETUP.md توضیح داده شده) و دوباره اجرا کن.\n"
            "→ اگه عمداً داری فقط رو سیستمِ خودت و بدونِ Postgres تست می‌کنی، این متغیر رو ست کن: "
            "ALLOW_SQLITE_FALLBACK=1"
        )
        return
    if pg_compat.postgres_enabled():
        logger.info("✅ دیتابیس: PostgreSQL (متغیرِ DATABASE_URL شناسایی شد) - همه‌چیز رو همینجا ذخیره می‌شه.")
    else:
        logger.warning("⚠️ دیتابیس: SQLiteِ محلی (ALLOW_SQLITE_FALLBACK=1 فعاله) - فقط برای تستِ محلی مناسبه.")
    init_db()
    app = build_application()
    if app.job_queue is not None:
        # هر ۳۰ دقیقه چک می‌کنه ببینه کدوم گروه‌ها وقتِ «یادآوری غیبت اعضا»شونه
        app.job_queue.run_repeating(engagement_job, interval=1800, first=60)
        # دقیقاً نصفه‌شب (۰۰:۰۰) به وقت تهران، سهمِ کسایی که «طرح روزانه»ی بانکِ پنالتی رو فعال کردن واریز می‌شه
        app.job_queue.run_daily(daily_bank_plan_job, time=dt_time(hour=0, minute=0, second=0, tzinfo=TEHRAN_TZ))
        # هر ۶۰ ثانیه چک می‌کنه کدوم گروه‌ها نوبتِ چالشِ ریاضیِ بعدیشونه (فاصله‌ی هر چالش ۱۰ تا ۲۰ دقیقه‌ست).
        # این مسیرِ اصلیه؛ یه مسیرِ پشتیبانِ مستقل هم رو خودِ پیام‌های گروه سوار شده (پایین‌تر ثبت می‌شه)
        # که حتی اگه به هر دلیلی جاب‌کیو کار نکنه، چالش‌ها بازم سرِ وقت بیان.
        app.job_queue.run_repeating(challenge_scheduler_job, interval=60, first=30)
        # هر ۵ دقیقه چک می‌کنه چالش‌های فعال نیاز به یادآوریِ ساعتی یا انقضای ۱۲ ساعته دارن یا نه
        app.job_queue.run_repeating(challenge_maintenance_job, interval=300, first=45)
        # هر ۳۰ دقیقه چک می‌کنه کدوم گروه‌ها نوبتِ سوالِ ویژه‌ی بعدیشونه (فاصله‌ی هر سوالِ ویژه ۸ تا ۱۲ ساعته)
        app.job_queue.run_repeating(bonus_challenge_scheduler_job, interval=1800, first=120)
        # هر ۳۰ دقیقه چک می‌کنه سوال‌های ویژه‌ی ۱۲ساعت‌بی‌جواب‌مونده رو بی‌سروصدا ببنده
        app.job_queue.run_repeating(bonus_challenge_maintenance_job, interval=1800, first=150)
    else:
        logger.warning(
            "JobQueue فعال نیست؛ برای قابلیت «یادآوری غیبت اعضا» این پکیج رو نصب کن: "
            "pip install \"python-telegram-bot[job-queue]==20.7\""
        )
    logger.info("ربات چتر در حال اجراست...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
