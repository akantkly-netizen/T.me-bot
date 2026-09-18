# -*- coding: utf-8 -*-
"""
دوز (تیک-تاک-تو) - ماژول مستقل بازی برای ربات چتر
=====================================================
این فایل کاملاً جداست تا مدیریتش راحت باشه؛ فقط کافیه تو bot.py این‌طور صداش بزنی:

    import dooz_game
    ...
    dooz_game.register(app)   # داخل build_application(), قبل از برگردوندن app

طراحی رابط کاربری: کل بازی روی یه پیامِ واحد انجام می‌شه (فقط ادیت می‌شه، نه پیام جدید)،
پس گپ همیشه تمیز می‌مونه - نیازی به تایپ عدد و پاک کردن پیام نیست.

قانون بازی: هر بازیکن حداکثر ۳ مهره رو زمین داره. وقتی مهره‌ی چهارم رو می‌ذاره، قدیمی‌ترین
مهره‌ی خودش (به ترتیب زمانی) برداشته می‌شه. این یعنی هیچ‌وقت بیشتر از ۶ خونه (۳+۳) پر نمی‌شه،
پس بازی هیچ‌وقت مساوی نمی‌شه - یا کسی می‌بره یا بازی ادامه داره.

قابلیت‌ها:
- "دوز" یا /dooz -> انتخاب حالت: بازی با ربات / بازی با کاربر / پروفایل
- بازی با ربات: ۴ درجه سختی (آسون تا وحشتناک)
- بازی با کاربر: روی پیام طرف ریپلای بزن و بنویس «بازی»
- بعد از باخت/برد، دکمه‌های «دوباره بازی» (با جابه‌جاییِ نفرِ اول) و «بستن بازی»
- آمار رودررو + پروفایل شخصی (جمع بازی/برد/باخت + لیست حریف‌ها)
"""

import os
import json
import random
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
# دیتابیس (همون فایل chatr_bot.db که کنار bot.py هست، یه جدول جدا برای بازی‌ها)
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("CHATR_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatr_bot.db"))
BOT_SENTINEL = -1  # آی‌دی قراردادی برای «ربات» به‌عنوان بازیکن (آی‌دی واقعی تلگرام هیچ‌وقت منفی نیست)
MAX_MARKS_PER_PLAYER = 3  # هر بازیکن حداکثر همین تعداد مهره رو زمین داره

WIN_LINES = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)

DIFFICULTY_LABELS = {
    "easy": "آسون",
    "normal": "معمولی",
    "hard": "سخت",
    "impossible": "وحشتناک",
}

VARIANT_LABELS = {
    "classic": "🎯 دوز کلاسیک",
    "limited": "🎲 دوز سه‌مهره‌ای",
}

MARK_X = "❌"
MARK_O = "⭕"
MARK_EMPTY = "・"


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
        CREATE TABLE IF NOT EXISTS dooz_games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            player_x_id INTEGER NOT NULL,
            player_x_name TEXT,
            player_o_id INTEGER,
            player_o_name TEXT,
            vs_bot INTEGER NOT NULL DEFAULT 0,
            difficulty TEXT,
            board TEXT NOT NULL,
            turn TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT,
            x_queue TEXT NOT NULL DEFAULT '[]',
            o_queue TEXT NOT NULL DEFAULT '[]',
            variant TEXT NOT NULL DEFAULT 'limited'
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS dooz_player_stats (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            first_seen TEXT,
            total_games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS dooz_head_to_head (
            user_a_id INTEGER NOT NULL,
            user_b_id INTEGER NOT NULL,
            user_a_name TEXT,
            user_b_name TEXT,
            wins_a INTEGER NOT NULL DEFAULT 0,
            wins_b INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_a_id, user_b_id)
        )
        """
    )
    conn.commit()

    # مهاجرت امن: اگه از قبل جدول dooz_games بدون x_queue/o_queue وجود داشته باشه
    c.execute("PRAGMA table_info(dooz_games)")
    existing_cols = [r[1] for r in c.fetchall()]
    for coldef in (
        "x_queue TEXT NOT NULL DEFAULT '[]'",
        "o_queue TEXT NOT NULL DEFAULT '[]'",
        "variant TEXT NOT NULL DEFAULT 'limited'",
    ):
        col_name = coldef.split()[0]
        if col_name not in existing_cols:
            c.execute(f"ALTER TABLE dooz_games ADD COLUMN {coldef}")
    conn.commit()
    conn.close()


_ensure_schema()


# ---------------------------------------------------------------------------
# لایه دیتابیس بازی
# ---------------------------------------------------------------------------
def create_game(chat_id, player_x_id, player_x_name, player_o_id, player_o_name, vs_bot, difficulty, board, turn, variant="limited"):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO dooz_games "
        "(chat_id, player_x_id, player_x_name, player_o_id, player_o_name, vs_bot, difficulty, board, turn, status, created_at, x_queue, o_queue, variant) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, '[]', '[]', ?)",
        (
            chat_id, player_x_id, player_x_name, player_o_id, player_o_name,
            int(vs_bot), difficulty, json.dumps(board), turn, datetime.utcnow().isoformat(), variant,
        ),
    )
    conn.commit()
    game_id = c.lastrowid
    conn.close()
    return game_id


def get_game(game_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM dooz_games WHERE game_id=?", (game_id,))
    row = c.fetchone()
    conn.close()
    return row


def set_message_id(game_id: int, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE dooz_games SET message_id=? WHERE game_id=?", (message_id, game_id))
    conn.commit()
    conn.close()


def update_game(game_id: int, board: list, turn: str, x_queue: list, o_queue: list):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE dooz_games SET board=?, turn=?, x_queue=?, o_queue=? WHERE game_id=?",
        (json.dumps(board), turn, json.dumps(x_queue), json.dumps(o_queue), game_id),
    )
    conn.commit()
    conn.close()


def finish_game(game_id: int, board: list):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE dooz_games SET board=?, status='finished' WHERE game_id=?", (json.dumps(board), game_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# آمار بازیکن‌ها (پروفایل) و رکورد رودررو بین دو نفر
# ---------------------------------------------------------------------------
def record_result_for_user(user_id: int, name: str, result: str):
    """result: 'win' | 'loss' | 'draw'"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM dooz_player_stats WHERE user_id=?", (user_id,))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO dooz_player_stats (user_id, name, first_seen, total_games, wins, losses, draws) "
            "VALUES (?, ?, ?, 0, 0, 0, 0)",
            (user_id, name, datetime.utcnow().isoformat()),
        )
    col = {"win": "wins", "loss": "losses", "draw": "draws"}[result]
    c.execute(
        f"UPDATE dooz_player_stats SET name=?, total_games=total_games+1, {col}={col}+1 WHERE user_id=?",
        (name, user_id),
    )
    conn.commit()
    conn.close()


def get_player_stats(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM dooz_player_stats WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def record_head_to_head(user_a_id, user_a_name, user_b_id, user_b_name, winner_id):
    """winner_id: آی‌دی برنده یا None برای مساوی. ترتیب a/b خودکار نرمال (کوچیک‌تر اول) می‌شه."""
    if user_a_id > user_b_id:
        user_a_id, user_b_id = user_b_id, user_a_id
        user_a_name, user_b_name = user_b_name, user_a_name

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM dooz_head_to_head WHERE user_a_id=? AND user_b_id=?", (user_a_id, user_b_id))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO dooz_head_to_head (user_a_id, user_b_id, user_a_name, user_b_name, wins_a, wins_b, draws, total) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, 0)",
            (user_a_id, user_b_id, user_a_name, user_b_name),
        )
    if winner_id is None:
        c.execute(
            "UPDATE dooz_head_to_head SET user_a_name=?, user_b_name=?, draws=draws+1, total=total+1 "
            "WHERE user_a_id=? AND user_b_id=?",
            (user_a_name, user_b_name, user_a_id, user_b_id),
        )
    elif winner_id == user_a_id:
        c.execute(
            "UPDATE dooz_head_to_head SET user_a_name=?, user_b_name=?, wins_a=wins_a+1, total=total+1 "
            "WHERE user_a_id=? AND user_b_id=?",
            (user_a_name, user_b_name, user_a_id, user_b_id),
        )
    else:
        c.execute(
            "UPDATE dooz_head_to_head SET user_a_name=?, user_b_name=?, wins_b=wins_b+1, total=total+1 "
            "WHERE user_a_id=? AND user_b_id=?",
            (user_a_name, user_b_name, user_a_id, user_b_id),
        )
    conn.commit()
    c.execute("SELECT * FROM dooz_head_to_head WHERE user_a_id=? AND user_b_id=?", (user_a_id, user_b_id))
    row = c.fetchone()
    conn.close()
    return row


def get_opponents_list(user_id: int):
    """لیست همه‌ی افرادی که این کاربر باهاشون بازیِ دونفره داشته، با آمار هرکدوم."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM dooz_head_to_head WHERE user_a_id=? OR user_b_id=?", (user_id, user_id))
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        if r["user_a_id"] == user_id:
            opp_name, wins, losses = r["user_b_name"], r["wins_a"], r["wins_b"]
        else:
            opp_name, wins, losses = r["user_a_name"], r["wins_b"], r["wins_a"]
        result.append({"name": opp_name, "wins": wins, "losses": losses, "draws": r["draws"], "total": r["total"]})
    result.sort(key=lambda x: x["total"], reverse=True)
    return result


def build_head_to_head_text(h2h_row) -> str:
    lines = ["", "📊 آمار رودررو:", f"کل بازی‌ها: {h2h_row['total']} بار",
             f"{h2h_row['user_a_name']}: {h2h_row['wins_a']} بار برد",
             f"{h2h_row['user_b_name']}: {h2h_row['wins_b']} بار برد"]
    if h2h_row["draws"]:
        lines.append(f"مساوی: {h2h_row['draws']} بار")
    return "\n".join(lines)


def finalize_game(game, board: list, winner: str) -> str:
    """بازی رو تموم‌شده علامت می‌زنه، آمار رو به‌روز می‌کنه، و متنِ آمارِ قابل‌نمایش رو برمی‌گردونه."""
    finish_game(game["game_id"], board)
    if game["vs_bot"]:
        uid, uname = game["player_x_id"], game["player_x_name"]
        if winner == "DRAW":
            record_result_for_user(uid, uname, "draw")
        elif winner == "X":
            record_result_for_user(uid, uname, "win")
        else:
            record_result_for_user(uid, uname, "loss")
        return ""

    x_id, x_name = game["player_x_id"], game["player_x_name"]
    o_id, o_name = game["player_o_id"], game["player_o_name"]
    if winner == "DRAW":
        record_result_for_user(x_id, x_name, "draw")
        record_result_for_user(o_id, o_name, "draw")
        h2h = record_head_to_head(x_id, x_name, o_id, o_name, winner_id=None)
    elif winner == "X":
        record_result_for_user(x_id, x_name, "win")
        record_result_for_user(o_id, o_name, "loss")
        h2h = record_head_to_head(x_id, x_name, o_id, o_name, winner_id=x_id)
    else:
        record_result_for_user(o_id, o_name, "win")
        record_result_for_user(x_id, x_name, "loss")
        h2h = record_head_to_head(x_id, x_name, o_id, o_name, winner_id=o_id)
    return "\n" + build_head_to_head_text(h2h)


# ---------------------------------------------------------------------------
# منطق بازی
# ---------------------------------------------------------------------------
def check_winner(board: list, variant: str = "limited"):
    """
    برگردوندن 'X'، 'O'، 'DRAW' یا None.
    - دوز سه‌مهره‌ای (limited): چون هیچ‌وقت بیشتر از ۶ خونه پر نمی‌شه، بازی هیچ‌وقت مساوی نمی‌شه.
    - دوز کلاسیک (classic): وقتی صفحه پر بشه و برنده‌ای نباشه، 'DRAW' برمی‌گرده.
    """
    for a, b, c_ in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c_]:
            return board[a]
    if variant == "classic" and all(cell != "" for cell in board):
        return "DRAW"
    return None


def apply_move(board: list, x_queue: list, o_queue: list, mark: str, idx: int, variant: str = "limited"):
    """
    یه حرکت رو اعمال می‌کنه.
    - دوز سه‌مهره‌ای: اگه بازیکن از قبل ۳ مهره رو زمین داشته باشه، قدیمی‌ترینش (طبق ترتیب
      زمانیِ صف) برداشته می‌شه و بعد مهره‌ی جدید تو خونه‌ی انتخابی گذاشته می‌شه.
    - دوز کلاسیک: مهره‌ها هیچ‌وقت برداشته نمی‌شن، درست مثل دوز سنتی.
    board / x_queue / o_queue همگی in-place تغییر می‌کنن.
    """
    queue = x_queue if mark == "X" else o_queue
    if variant == "limited" and len(queue) >= MAX_MARKS_PER_PLAYER:
        oldest = queue.pop(0)
        board[oldest] = ""
    queue.append(idx)
    board[idx] = mark


def _heuristic_score(board: list, bot_mark: str, human_mark: str) -> int:
    """امتیازدهیِ تقریبی برای وقتی جستجو به عمقِ مجاز رسیده ولی بازی تموم نشده."""
    score = 0
    for a, b, c_ in WIN_LINES:
        line = (board[a], board[b], board[c_])
        bot_count = line.count(bot_mark)
        human_count = line.count(human_mark)
        if bot_count and not human_count:
            score += 10 ** bot_count
        elif human_count and not bot_count:
            score -= 10 ** human_count
    return score


def minimax_limited(board, x_queue, o_queue, player, bot_mark, human_mark, depth, max_depth, alpha, beta, variant="limited"):
    """
    مینی‌مکسِ عمق‌محدود با هرسِ آلفا-بتا.
    - دوز سه‌مهره‌ای: چون مهره‌ها می‌چرخن بازی می‌تونه نظری تا بی‌نهایت ادامه پیدا کنه، پس یه
      جستجوی عمیقِ کافی + هیوریستیک خوب داریم که در عمل حریف رو خیلی سخت می‌کنه.
    - دوز کلاسیک: صفحه فقط ۹ خونه داره و مهره‌ای برداشته نمی‌شه، پس با max_depth=9 این یه
      مینی‌مکسِ کاملاً حل‌شده (بدون شکست) می‌شه.
    """
    winner = check_winner(board, variant)
    if winner == bot_mark:
        return 1000 - depth, None
    if winner == human_mark:
        return depth - 1000, None
    if winner == "DRAW":
        return 0, None
    if depth >= max_depth:
        return _heuristic_score(board, bot_mark, human_mark), None

    empty = [i for i, v in enumerate(board) if v == ""]
    best_move = None
    if player == bot_mark:
        best_score = float("-inf")
        for i in empty:
            b2, xq2, oq2 = board[:], x_queue[:], o_queue[:]
            apply_move(b2, xq2, oq2, player, i, variant)
            score, _ = minimax_limited(b2, xq2, oq2, human_mark, bot_mark, human_mark, depth + 1, max_depth, alpha, beta, variant)
            if score > best_score:
                best_score, best_move = score, i
            alpha = max(alpha, best_score)
            if alpha >= beta:
                break
        return best_score, best_move
    else:
        best_score = float("inf")
        for i in empty:
            b2, xq2, oq2 = board[:], x_queue[:], o_queue[:]
            apply_move(b2, xq2, oq2, player, i, variant)
            score, _ = minimax_limited(b2, xq2, oq2, bot_mark, bot_mark, human_mark, depth + 1, max_depth, alpha, beta, variant)
            if score < best_score:
                best_score, best_move = score, i
            beta = min(beta, best_score)
            if alpha >= beta:
                break
        return best_score, best_move


def bot_move(board: list, x_queue: list, o_queue: list, difficulty: str, bot_mark: str, human_mark: str, variant: str = "limited"):
    empty = [i for i, v in enumerate(board) if v == ""]
    if not empty:
        return None

    if difficulty == "easy":
        return random.choice(empty)

    if difficulty == "normal":
        # اگه یه حرکت برای بردن هست، بزن
        for i in empty:
            b2, xq2, oq2 = board[:], x_queue[:], o_queue[:]
            apply_move(b2, xq2, oq2, bot_mark, i, variant)
            if check_winner(b2, variant) == bot_mark:
                return i
        # اگه حریف داره می‌بره، جلوشو بگیر
        for i in empty:
            b2, xq2, oq2 = board[:], x_queue[:], o_queue[:]
            apply_move(b2, xq2, oq2, human_mark, i, variant)
            if check_winner(b2, variant) == human_mark:
                return i
        # وگرنه اولویت با وسط، بعد گوشه‌ها، بعد رندوم
        for pref in (4, 0, 2, 6, 8):
            if pref in empty:
                return pref
        return random.choice(empty)

    # دوز کلاسیک فقط ۹ خونه داره، پس می‌شه با max_depth=9 یه جستجوی کامل (بدون شکست) زد.
    # دوز سه‌مهره‌ای چون نظری بی‌نهایته، عمق محدودتر (۳ برای سخت، ۶ برای وحشتناک) کافیه.
    max_depth = 9 if variant == "classic" else (3 if difficulty == "hard" else 6)

    if difficulty == "hard":
        # ۱۵٪ مواقع یه حرکت غیربهینه می‌زنه تا قابل‌شکست باشه، ولی بازم قویه
        if random.random() < 0.15:
            return random.choice(empty)
        _, move = minimax_limited(board[:], x_queue[:], o_queue[:], bot_mark, bot_mark, human_mark, 0, max_depth, float("-inf"), float("inf"), variant)
        return move if move is not None else random.choice(empty)

    # impossible: جستجوی عمیق‌تر، بدون شانس تصادفی - در دوز کلاسیک عملاً غیرقابل‌شکسته
    _, move = minimax_limited(board[:], x_queue[:], o_queue[:], bot_mark, bot_mark, human_mark, 0, max_depth, float("-inf"), float("inf"), variant)
    return move if move is not None else random.choice(empty)


# ---------------------------------------------------------------------------
# رندر پیام بازی (متن + کیبورد شیشه‌ای)
# ---------------------------------------------------------------------------
def _player_labels(game):
    x_label = f"{MARK_X} {game['player_x_name'] or 'بازیکن ۱'}"
    if game["vs_bot"]:
        diff_fa = DIFFICULTY_LABELS.get(game["difficulty"], "")
        o_label = f"{MARK_O} ربات ({diff_fa})"
    else:
        o_label = f"{MARK_O} {game['player_o_name'] or 'بازیکن ۲'}"
    return x_label, o_label


def build_game_view(game_id: int, board_override=None, winner=None):
    game = get_game(game_id)
    board = board_override if board_override is not None else json.loads(game["board"])
    x_label, o_label = _player_labels(game)
    variant = game["variant"] if "variant" in game.keys() else "limited"

    lines = [f"🎮 {VARIANT_LABELS.get(variant, 'دوز')}", f"{x_label}   در مقابل   {o_label}", ""]
    if winner:
        if winner == "DRAW":
            lines.append("🤝 مساوی شد! بازی خوبی بود.")
        elif winner == "X":
            lines.append(f"🎉 برد با {x_label}!")
        else:
            lines.append(f"🎉 برد با {o_label}!")
    else:
        turn_label = x_label if game["turn"] == "X" else o_label
        lines.append(f"نوبت: {turn_label}")
    text = "\n".join(lines)

    buttons = []
    for r in range(3):
        row = []
        for col in range(3):
            i = r * 3 + col
            cell = board[i]
            cell_text = MARK_X if cell == "X" else MARK_O if cell == "O" else MARK_EMPTY
            row.append(InlineKeyboardButton(cell_text, callback_data=f"dooz_move_{game_id}_{i}"))
        buttons.append(row)
    if winner:
        buttons.append([InlineKeyboardButton("🔁 دوباره بازی", callback_data=f"dooz_rematch_{game_id}")])
        buttons.append([InlineKeyboardButton("🗑 بستن بازی", callback_data=f"dooz_close_{game_id}")])
    else:
        buttons.append([InlineKeyboardButton("🔽 پایین بردن", callback_data=f"dooz_bump_{game_id}")])
    return text, InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------
# وقتی کاربر "👥 بازی با کاربر" رو برای یه نوع خاص (کلاسیک/سه‌مهره‌ای) می‌زنه، تا وقتی که
# طرفِ مقابل رو با ریپلای «بازی» مشخص کنه، انتخابش تو game_router نگه داشته می‌شه.


def type_menu_keyboard():
    return _type_menu_keyboard()


def _type_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(VARIANT_LABELS["classic"], callback_data="dooz_type_classic")],
            [InlineKeyboardButton(VARIANT_LABELS["limited"], callback_data="dooz_type_limited")],
            [InlineKeyboardButton("👤 پروفایل", callback_data="dooz_profile")],
        ]
    )


def _mode_menu_keyboard(variant: str):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🤖 بازی با ربات", callback_data=f"dooz_mode_bot_{variant}")],
            [InlineKeyboardButton("👥 بازی با کاربر", callback_data=f"dooz_mode_user_{variant}")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="dooz_back_main")],
        ]
    )


async def dooz_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎮 دوز! اول نوعش رو انتخاب کن:", reply_markup=_type_menu_keyboard())


async def dooz_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """انتخاب نوع بازی: کلاسیک (نامحدود) یا سه‌مهره‌ای. بعدش می‌پرسه با ربات یا با کاربر."""
    query = update.callback_query
    await query.answer()
    variant = query.data.split("_")[-1]  # classic | limited
    await query.edit_message_text(
        f"{VARIANT_LABELS.get(variant, 'دوز')} رو انتخاب کردی. حالا حالتت رو انتخاب کن:",
        reply_markup=_mode_menu_keyboard(variant),
    )


async def dooz_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data: dooz_mode_bot_classic | dooz_mode_bot_limited | dooz_mode_user_classic | dooz_mode_user_limited
    parts = query.data.split("_")
    mode, variant = parts[2], parts[3]
    if mode == "bot":
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("😌 آسون", callback_data=f"dooz_diff_easy_{variant}"),
                 InlineKeyboardButton("🙂 معمولی", callback_data=f"dooz_diff_normal_{variant}")],
                [InlineKeyboardButton("😈 سخت", callback_data=f"dooz_diff_hard_{variant}"),
                 InlineKeyboardButton("💀 وحشتناک", callback_data=f"dooz_diff_impossible_{variant}")],
                [InlineKeyboardButton("⬅️ بازگشت", callback_data=f"dooz_type_{variant}")],
            ]
        )
        await query.edit_message_text("درجه سختی رو انتخاب کن:", reply_markup=keyboard)
    else:
        game_router.set_pending(update.effective_user.id, "dooz", variant)
        await query.edit_message_text(
            f"{VARIANT_LABELS.get(variant, 'دوز')} انتخاب شد.\n"
            "با اون کسی که می‌خوای باهاش بازی کنی، روی یکی از پیام‌هاش ریپلای بزن و بنویس «بازی»."
        )


async def dooz_diff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data: dooz_diff_<difficulty>_<variant>
    parts = query.data.split("_")
    difficulty, variant = parts[2], parts[3]
    user = update.effective_user
    board = [""] * 9
    game_id = create_game(
        chat_id=update.effective_chat.id,
        player_x_id=user.id,
        player_x_name=user.first_name or "بازیکن",
        player_o_id=BOT_SENTINEL,
        player_o_name="ربات",
        vs_bot=True,
        difficulty=difficulty,
        board=board,
        turn="X",
        variant=variant,
    )
    text, keyboard = build_game_view(game_id)
    await query.edit_message_text(text, reply_markup=keyboard)
    set_message_id(game_id, query.message.message_id)


async def dooz_challenge_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE, forced_variant: str = "limited"):
    """وقتی کسی روی پیام یکی دیگه ریپلای بزنه و بنویسه «بازی» (یا مستقیم «کلاسیک»/«سه‌مهره‌ای»)."""
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

    variant = forced_variant
    board = [""] * 9
    game_id = create_game(
        chat_id=update.effective_chat.id,
        player_x_id=challenger.id,
        player_x_name=challenger.first_name or "بازیکن ۱",
        player_o_id=opponent.id,
        player_o_name=opponent.first_name or "بازیکن ۲",
        vs_bot=False,
        difficulty=None,
        board=board,
        turn="X",
        variant=variant,
    )
    text, keyboard = build_game_view(game_id)
    sent = await msg.reply_text(text, reply_markup=keyboard)
    set_message_id(game_id, sent.message_id)


async def dooz_challenge_classic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ریپلای رو پیام کسی + نوشتن «کلاسیک» = مستقیم یه بازیِ کلاسیک باهاش شروع می‌شه، بدون دکمه."""
    await dooz_challenge_trigger(update, context, forced_variant="classic")


async def dooz_challenge_limited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ریپلای رو پیام کسی + نوشتن «سه‌مهره‌ای» = مستقیم یه بازیِ سه‌مهره‌ای باهاش شروع می‌شه، بدون دکمه."""
    await dooz_challenge_trigger(update, context, forced_variant="limited")


async def dooz_move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    game_id = int(parts[2])
    idx = int(parts[3])

    game = get_game(game_id)
    if game is None or game["status"] != "active":
        await query.answer("این بازی تموم شده. اگه می‌خوای، دکمه‌ی «دوباره بازی» رو بزن.", show_alert=True)
        return

    board = json.loads(game["board"])
    x_queue = json.loads(game["x_queue"])
    o_queue = json.loads(game["o_queue"])
    user = update.effective_user
    current_mark = game["turn"]
    current_player_id = game["player_x_id"] if current_mark == "X" else game["player_o_id"]
    variant = game["variant"] if "variant" in game.keys() else "limited"

    if user.id != current_player_id:
        await query.answer("الان نوبت تو نیست!", show_alert=True)
        return
    if board[idx] != "":
        await query.answer("این خونه پره!", show_alert=True)
        return

    await query.answer()
    apply_move(board, x_queue, o_queue, current_mark, idx, variant)
    winner = check_winner(board, variant)
    if winner:
        stats_text = finalize_game(game, board, winner)
        text, keyboard = build_game_view(game_id, board_override=board, winner=winner)
        await query.edit_message_text(text + stats_text, reply_markup=keyboard)
        return

    next_mark = "O" if current_mark == "X" else "X"
    update_game(game_id, board, next_mark, x_queue, o_queue)

    # اگه بازی با رباته و نوبت به ربات (O) رسید، همین‌جا حرکتشو می‌زنه - بدون معطلی
    if game["vs_bot"] and next_mark == "O":
        move_idx = bot_move(board[:], x_queue[:], o_queue[:], game["difficulty"], bot_mark="O", human_mark="X", variant=variant)
        if move_idx is not None:
            apply_move(board, x_queue, o_queue, "O", move_idx, variant)
        winner2 = check_winner(board, variant)
        if winner2:
            stats_text = finalize_game(game, board, winner2)
            text, keyboard = build_game_view(game_id, board_override=board, winner=winner2)
            await query.edit_message_text(text + stats_text, reply_markup=keyboard)
            return
        update_game(game_id, board, "X", x_queue, o_queue)

    text, keyboard = build_game_view(game_id, board_override=board)
    await query.edit_message_text(text, reply_markup=keyboard)


async def dooz_rematch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    old_game_id = int(query.data.split("_")[-1])
    old = get_game(old_game_id)
    if old is None:
        await query.answer("بازی قبلی پیدا نشد.", show_alert=True)
        return
    await query.answer()

    board = [""] * 9
    variant = old["variant"] if "variant" in old.keys() else "limited"
    if old["vs_bot"]:
        new_id = create_game(
            chat_id=old["chat_id"],
            player_x_id=old["player_x_id"],
            player_x_name=old["player_x_name"],
            player_o_id=BOT_SENTINEL,
            player_o_name="ربات",
            vs_bot=True,
            difficulty=old["difficulty"],
            board=board,
            turn="X",
            variant=variant,
        )
    else:
        # نفر اولِ دفعه‌ی قبل، این‌بار دومی می‌شه - منصفانه‌تره
        new_id = create_game(
            chat_id=old["chat_id"],
            player_x_id=old["player_o_id"],
            player_x_name=old["player_o_name"],
            player_o_id=old["player_x_id"],
            player_o_name=old["player_x_name"],
            vs_bot=False,
            difficulty=None,
            board=board,
            turn="X",
            variant=variant,
        )
    text, keyboard = build_game_view(new_id)
    await query.edit_message_text(text, reply_markup=keyboard)
    set_message_id(new_id, query.message.message_id)


async def dooz_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بستن (حذف) پیامِ بازی - چون پیام مشترکه، حذفش برای هر دو طرف یکسان اعمال می‌شه."""
    query = update.callback_query
    game_id = int(query.data.split("_")[-1])
    game = get_game(game_id)
    user_id = update.effective_user.id
    if game is not None:
        participant_ids = {game["player_x_id"], game["player_o_id"]}
        if user_id not in participant_ids:
            await query.answer("این بازی مال تو نیست، فقط بازیکن‌ها می‌تونن ببندنش.", show_alert=True)
            return
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        await query.edit_message_text("بازی بسته شد. ✅")


async def dooz_bump_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیامِ فعلیِ بازی رو حذف می‌کنه و همون بازی رو تو یه پیامِ تازه (پایینِ گفتگو) دوباره می‌فرسته -
    برای وقتی که بین حرف‌های گروه، بازی رفته بالا و پیداش سخت شده."""
    query = update.callback_query
    game_id = int(query.data.split("_")[-1])
    game = get_game(game_id)
    if game is None or game["status"] != "active":
        await query.answer("این بازی دیگه فعال نیست.", show_alert=True)
        return
    user_id = update.effective_user.id
    if user_id not in {game["player_x_id"], game["player_o_id"]}:
        await query.answer("این بازی مال تو نیست، فقط بازیکن‌ها می‌تونن پایینش بیارن.", show_alert=True)
        return
    await query.answer()
    text, keyboard = build_game_view(game_id)
    try:
        sent = await context.bot.send_message(update.effective_chat.id, text, reply_markup=keyboard)
    except Exception:
        return
    set_message_id(game_id, sent.message_id)
    try:
        await query.message.delete()
    except Exception:
        pass


async def dooz_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    stats = get_player_stats(user.id)
    lines = [f"👤 پروفایل {user.first_name or 'بازیکن'}", ""]
    if stats is None:
        lines.append("هنوز هیچ بازی دوزی نکردی! یه بازی شروع کن ببین چند تا می‌بری 😉")
    else:
        first_seen = (stats["first_seen"] or "")[:10]
        lines.append(f"تاریخ عضویت (اولین بازی): {first_seen}")
        lines.append(f"جمع کل بازی‌ها: {stats['total_games']}")
        lines.append(f"جمع کل بردها: {stats['wins']}")
        lines.append(f"جمع کل باخت‌ها: {stats['losses']}")
        if stats["draws"]:
            lines.append(f"مساوی‌ها: {stats['draws']}")
        lines.append("")
        opponents = get_opponents_list(user.id)
        if opponents:
            lines.append("👥 کسایی که باهاشون بازی کردی:")
            for opp in opponents:
                extra = f"، {opp['draws']} مساوی" if opp["draws"] else ""
                lines.append(f"- {opp['name']}: {opp['total']} بازی ({opp['wins']} برد، {opp['losses']} باخت{extra})")
        else:
            lines.append("هنوز با کسی بازیِ دونفره نداشتی.")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="dooz_back_main")]])
    await query.edit_message_text("\n".join(lines), reply_markup=keyboard)


async def dooz_back_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🎮 دوز! اول نوعش رو انتخاب کن:", reply_markup=_type_menu_keyboard())


# ---------------------------------------------------------------------------
# ثبت هندلرها - این تابع رو از bot.py صدا بزن: dooz_game.register(app)
# ---------------------------------------------------------------------------
def register(app: Application):
    # گروه ۲: مستقل از هندلر عمومی متنیِ گروه (group=0) اجرا می‌شه، پس با هم تداخل ندارن
    app.add_handler(CommandHandler("dooz", dooz_entry), group=2)
    app.add_handler(
        MessageHandler(filters.Regex(r"^دوز$") & (filters.ChatType.GROUPS | filters.ChatType.PRIVATE), dooz_entry),
        group=2,
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^کلاسیک$") & filters.REPLY, dooz_challenge_classic),
        group=2,
    )
    app.add_handler(
        MessageHandler(filters.Regex(r"^سه[\s‌]*مهره(?:[\s‌]*ای)?$") & filters.REPLY, dooz_challenge_limited),
        group=2,
    )
    app.add_handler(CallbackQueryHandler(dooz_type_callback, pattern="^dooz_type_(classic|limited)$"))
    app.add_handler(CallbackQueryHandler(dooz_mode_callback, pattern="^dooz_mode_"))
    app.add_handler(CallbackQueryHandler(dooz_diff_callback, pattern="^dooz_diff_"))
    app.add_handler(CallbackQueryHandler(dooz_move_callback, pattern="^dooz_move_"))
    app.add_handler(CallbackQueryHandler(dooz_rematch_callback, pattern="^dooz_rematch_"))
    app.add_handler(CallbackQueryHandler(dooz_close_callback, pattern="^dooz_close_"))
    app.add_handler(CallbackQueryHandler(dooz_bump_callback, pattern="^dooz_bump_"))
    app.add_handler(CallbackQueryHandler(dooz_profile_callback, pattern="^dooz_profile$"))
    app.add_handler(CallbackQueryHandler(dooz_back_main_callback, pattern="^dooz_back_main$"))
