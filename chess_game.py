# -*- coding: utf-8 -*-
"""
بازی شطرنج - ماژول کاملاً جدا، کنارِ bot.py/dooz_game.py/rps_game.py قرار می‌گیره.
موتورِ قوانین تو chess_rules.py هست (تست‌شده با Perft تا عمقِ ۴ - عددهای رسمی و شناخته‌شده‌ی
تستِ موتورهای شطرنج)؛ این فایل فقط لایه‌ی دیتابیس + تلگرام + عنوان/پروفایل رو اضافه می‌کنه.

ساده‌سازیِ عمدی: پروموشنِ پیاده همیشه خودکار به وزیر تبدیل می‌شه (auto-queen) - رایج‌ترین
انتخابه و از یه لایه‌ی UI اضافه (پرسیدنِ نوعِ مهره) صرف‌نظر می‌کنه.
"""
import os
import json
from datetime import datetime

import game_router
import chess_rules as cr

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

import sqlite3

DB_PATH = os.environ.get("CHATR_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chatr_bot.db"))

try:
    import pg_compat
except ImportError:
    pg_compat = None

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
        if pg_compat is not None:
            pg_url = pg_compat.postgres_url()
            if pg_url:
                _DB_CONN = pg_compat.connect(pg_url)
        if _DB_CONN is None:
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
        CREATE TABLE IF NOT EXISTS chess_games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            white_id INTEGER NOT NULL,
            white_name TEXT,
            black_id INTEGER NOT NULL,
            black_name TEXT,
            vs_bot INTEGER NOT NULL DEFAULT 0,
            difficulty TEXT,
            state_json TEXT NOT NULL,
            selected_square INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chess_player_stats (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            total_games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            user_wins INTEGER NOT NULL DEFAULT 0,
            bot_wins INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chess_head_to_head (
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


_ensure_schema()

DIFFICULTY_PARAMS = {
    "easy": {"depth": 1, "randomness": 0.6, "label": "🟢 آسون"},
    "medium": {"depth": 2, "randomness": 0.25, "label": "🟡 متوسط"},
    "hard": {"depth": 3, "randomness": 0.0, "label": "🔴 سخت"},
}

USER_TITLES = [(30, "♔ استاد شطرنج"), (15, "♘ شوالیه شطرنج"), (5, "♙ شاگرد شطرنج")]
BOT_TITLES = [(30, "🏆 اسطوره‌ی شطرنج"), (15, "🏅 قهرمان افسانه‌ای"), (5, "🎖 استاد ویژه")]


def get_user_title(wins_vs_user: int):
    for threshold, title in USER_TITLES:
        if wins_vs_user >= threshold:
            return title
    return None


def get_bot_title(wins_vs_bot: int):
    for threshold, title in BOT_TITLES:
        if wins_vs_bot >= threshold:
            return title
    return None


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
def create_game(chat_id, white_id, white_name, black_id, black_name, vs_bot, difficulty):
    conn = get_conn()
    c = conn.cursor()
    state = cr.new_game_state()
    c.execute(
        "INSERT INTO chess_games (chat_id, white_id, white_name, black_id, black_name, vs_bot, difficulty, "
        "state_json, selected_square, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?)",
        (chat_id, white_id, white_name, black_id, black_name, int(vs_bot), difficulty, json.dumps(state), datetime.utcnow().isoformat()),
    )
    conn.commit()
    game_id = c.lastrowid
    return game_id


def get_game(game_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM chess_games WHERE game_id=?", (game_id,))
    return c.fetchone()


def save_game_state(game_id: int, state, selected_square=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE chess_games SET state_json=?, selected_square=? WHERE game_id=?",
        (json.dumps(state), selected_square, game_id),
    )
    conn.commit()


def set_message_id(game_id: int, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE chess_games SET message_id=? WHERE game_id=?", (message_id, game_id))
    conn.commit()


def finish_game(game_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE chess_games SET status='finished' WHERE game_id=?", (game_id,))
    conn.commit()


def record_result_for_user(user_id, name, outcome, vs_bot):
    """outcome: 'win' | 'loss' | 'draw'. برمی‌گردونه (لقبِ قبلی, لقبِ جدید) برای تشخیصِ ارتقای لقب."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM chess_player_stats WHERE user_id=?", (user_id,))
    before = c.fetchone()
    prev_user_wins = before["user_wins"] if before else 0
    prev_bot_wins = before["bot_wins"] if before else 0

    win_inc = 1 if outcome == "win" else 0
    loss_inc = 1 if outcome == "loss" else 0
    draw_inc = 1 if outcome == "draw" else 0
    bot_win_inc = 1 if (outcome == "win" and vs_bot) else 0
    user_win_inc = 1 if (outcome == "win" and not vs_bot) else 0

    c.execute(
        "INSERT INTO chess_player_stats (user_id, name, total_games, wins, losses, draws, user_wins, bot_wins) "
        "VALUES (?, ?, 1, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, total_games=total_games+1, "
        "wins=wins+excluded.wins, losses=losses+excluded.losses, draws=draws+excluded.draws, "
        "user_wins=user_wins+excluded.user_wins, bot_wins=bot_wins+excluded.bot_wins",
        (user_id, name, win_inc, loss_inc, draw_inc, user_win_inc, bot_win_inc),
    )
    conn.commit()

    new_user_wins = prev_user_wins + user_win_inc
    new_bot_wins = prev_bot_wins + bot_win_inc
    old_title = get_user_title(prev_user_wins) if not vs_bot else get_bot_title(prev_bot_wins)
    new_title = get_user_title(new_user_wins) if not vs_bot else get_bot_title(new_bot_wins)
    return (old_title, new_title) if new_title != old_title else (None, None)


def get_player_stats(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM chess_player_stats WHERE user_id=?", (user_id,))
    return c.fetchone()


def record_head_to_head(user_a_id, user_a_name, user_b_id, user_b_name, winner_id):
    if user_a_id > user_b_id:
        user_a_id, user_b_id = user_b_id, user_a_id
        user_a_name, user_b_name = user_b_name, user_a_name
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM chess_head_to_head WHERE user_a_id=? AND user_b_id=?", (user_a_id, user_b_id))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO chess_head_to_head (user_a_id, user_b_id, user_a_name, user_b_name, wins_a, wins_b, draws, total) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, 0)",
            (user_a_id, user_b_id, user_a_name, user_b_name),
        )
    if winner_id is None:
        c.execute(
            "UPDATE chess_head_to_head SET user_a_name=?, user_b_name=?, draws=draws+1, total=total+1 WHERE user_a_id=? AND user_b_id=?",
            (user_a_name, user_b_name, user_a_id, user_b_id),
        )
    elif winner_id == user_a_id:
        c.execute(
            "UPDATE chess_head_to_head SET user_a_name=?, user_b_name=?, wins_a=wins_a+1, total=total+1 WHERE user_a_id=? AND user_b_id=?",
            (user_a_name, user_b_name, user_a_id, user_b_id),
        )
    else:
        c.execute(
            "UPDATE chess_head_to_head SET user_a_name=?, user_b_name=?, wins_b=wins_b+1, total=total+1 WHERE user_a_id=? AND user_b_id=?",
            (user_a_name, user_b_name, user_a_id, user_b_id),
        )
    conn.commit()


def get_opponents_list(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM chess_head_to_head WHERE user_a_id=? OR user_b_id=?", (user_id, user_id))
    rows = c.fetchall()
    result = []
    for r in rows:
        if r["user_a_id"] == user_id:
            opp_name, wins, losses = r["user_b_name"], r["wins_a"], r["wins_b"]
        else:
            opp_name, wins, losses = r["user_a_name"], r["wins_b"], r["wins_a"]
        result.append({"name": opp_name, "wins": wins, "losses": losses, "draws": r["draws"], "total": r["total"]})
    result.sort(key=lambda x: x["total"], reverse=True)
    return result


def finalize_game(game, state, status):
    """بازی رو تموم‌شده علامت می‌زنه، آمار/عنوان/رودررو رو به‌روز می‌کنه. برمی‌گردونه متنِ نتیجه."""
    finish_game(game["game_id"])
    w_id, w_name = game["white_id"], game["white_name"]
    b_id, b_name = game["black_id"], game["black_name"]
    vs_bot = bool(game["vs_bot"])

    if status == "checkmate":
        winner_color = "b" if state["turn"] == "w" else "w"  # هرکی نوبتشه کیش‌ومات شده، پس حریفش برده
    else:
        winner_color = None  # مساوی (استیل‌میت یا سایرِ حالت‌های مساوی)

    lines = []
    if winner_color == "w":
        outcome_w, outcome_b = "win", "loss"
        lines.append(f"🏆 برد با {w_name} (سفید)! کیش و مات.")
    elif winner_color == "b":
        outcome_w, outcome_b = "loss", "win"
        lines.append(f"🏆 برد با {b_name} (سیاه)! کیش و مات.")
    else:
        outcome_w = outcome_b = "draw"
        reason = {"stalemate": "استیل‌میت", "draw_50move": "قانونِ ۵۰ حرکت", "draw_repetition": "تکرارِ سه‌باره", "draw_material": "کمبودِ مهره برای مات‌کردن"}.get(status, "")
        lines.append(f"🤝 بازی مساوی شد ({reason}).")

    old_w, new_w = record_result_for_user(w_id, w_name, outcome_w, vs_bot)
    if not vs_bot:
        old_b, new_b = record_result_for_user(b_id, b_name, outcome_b, vs_bot)
        record_head_to_head(w_id, w_name, b_id, b_name, winner_id=(w_id if winner_color == "w" else (b_id if winner_color == "b" else None)))
        if new_w:
            lines.append(f"🎉 {w_name} به لقبِ «{new_w}» رسید!")
        if new_b:
            lines.append(f"🎉 {b_name} به لقبِ «{new_b}» رسید!")
    else:
        # تو حالتِ ربات، فقط طرفِ انسان (که همیشه سفیده) رکورد می‌شه
        if new_w:
            lines.append(f"🎉 {w_name} به لقبِ «{new_w}» رسید!")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# رندرِ صفحه
# ---------------------------------------------------------------------------
def _square_label(state, sq, selected_sq, legal_dest_squares):
    piece = state["board"][sq]
    symbol = cr.PIECE_UNICODE.get(piece, "")
    if sq == selected_sq:
        return f"[{symbol}]" if symbol else "[ ]"
    if sq in legal_dest_squares:
        return f"({symbol})" if symbol else " • "
    if symbol:
        return f" {symbol} "
    return "    "  # خونه‌ی خالیِ معمولی - دیگه نقطه نداره؛ نقطه فقط بعد از انتخابِ یه مهره، رو مقصدهای مجازش دیده می‌شه


def _render_order(turn_color: str):
    """صفحه رو طوری می‌چینه که مهره‌های نوبت‌دار پایینِ صفحه باشن (دقیقاً مثلِ رو-میزی که
    هر بار جلوی بازیکنِ نوبت‌دار می‌چرخونیمش) - سفید: ردیفِ ۸ بالا/۱ پایین، ستون‌ها a→h؛
    سیاه: کاملاً برعکس (چرخشِ ۱۸۰ درجه)."""
    if turn_color == "w":
        return range(7, -1, -1), range(0, 8)
    return range(0, 8), range(7, -1, -1)


def build_board_view(game_id: int):
    game = get_game(game_id)
    state = json.loads(game["state_json"])
    selected_sq = game["selected_square"]
    legal_dest = set()
    if selected_sq is not None:
        legal_dest = {m["to"] for m in cr.legal_moves_for_square(state, selected_sq)}

    status = cr.game_status(state)
    turn_label = "⚪ سفید" if state["turn"] == "w" else "⚫ سیاه"
    lines = [
        "♟️ شطرنج",
        f"⚪ {game['white_name']}   در مقابل   ⚫ {game['black_name']}",
        "",
    ]
    if status == "ongoing":
        check_note = " (کیش!)" if cr.is_in_check(state) else ""
        lines.append(f"نوبت: {turn_label}{check_note} (تخته رو به سمتِ همینه)")
    lines.append("")
    text = "\n".join(lines)

    ranks, files = _render_order(state["turn"])
    buttons = []
    for rank in ranks:
        row = []
        for file in files:
            sq = rank * 8 + file
            row.append(InlineKeyboardButton(_square_label(state, sq, selected_sq, legal_dest), callback_data=f"chess_sq_{game_id}_{sq}"))
        buttons.append(row)

    if status != "ongoing":
        buttons.append([InlineKeyboardButton("🔁 دوباره بازی", callback_data=f"chess_rematch_{game_id}")])
        buttons.append([InlineKeyboardButton("🗑 بستن", callback_data=f"chess_close_{game_id}")])
    else:
        buttons.append([InlineKeyboardButton("🔽 پایین بردن", callback_data=f"chess_bump_{game_id}")])

    return text, InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# منوها
# ---------------------------------------------------------------------------
def mode_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 بازی با کاربر", callback_data="chess_mode_user")],
            [InlineKeyboardButton("🤖 بازی با ربات", callback_data="chess_mode_bot")],
            [InlineKeyboardButton("👤 پروفایل", callback_data="chess_profile")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="games_back_main")],
        ]
    )


def _difficulty_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(DIFFICULTY_PARAMS["easy"]["label"], callback_data="chess_diff_easy")],
            [InlineKeyboardButton(DIFFICULTY_PARAMS["medium"]["label"], callback_data="chess_diff_medium")],
            [InlineKeyboardButton(DIFFICULTY_PARAMS["hard"]["label"], callback_data="chess_diff_hard")],
        ]
    )


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------
async def chess_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("_")[-1]
    if mode == "bot":
        await query.edit_message_text("درجه‌ی سختی رو انتخاب کن:", reply_markup=_difficulty_keyboard())
    else:
        game_router.set_pending(update.effective_user.id, "chess")
        await query.edit_message_text(
            "با اون کسی که می‌خوای باهاش بازی کنی، روی یکی از پیام‌هاش تو گروه ریپلای بزن و بنویس «بازی».\n"
            "(تو همیشه با مهره‌های سفید شروع می‌کنی)"
        )


async def chess_diff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    difficulty = query.data.split("_")[-1]
    user = update.effective_user
    game_id = create_game(
        chat_id=update.effective_chat.id,
        white_id=user.id, white_name=user.first_name or "بازیکن",
        black_id=0, black_name="ربات",
        vs_bot=True, difficulty=difficulty,
    )
    text, keyboard = build_board_view(game_id)
    await query.edit_message_text(text, reply_markup=keyboard)


async def chess_challenge_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        white_id=challenger.id, white_name=challenger.first_name or "بازیکن ۱",
        black_id=opponent.id, black_name=opponent.first_name or "بازیکن ۲",
        vs_bot=False, difficulty=None,
    )
    text, keyboard = build_board_view(game_id)
    sent = await msg.reply_text(text, reply_markup=keyboard)
    set_message_id(game_id, sent.message_id)


async def _maybe_finish_and_render(query, game_id):
    game = get_game(game_id)
    state = json.loads(game["state_json"])
    status = cr.game_status(state)
    if status != "ongoing":
        result_text = finalize_game(game, state, status)
        text, keyboard = build_board_view(game_id)
        await query.edit_message_text(text + "\n" + result_text, reply_markup=keyboard)
        return True
    text, keyboard = build_board_view(game_id)
    await query.edit_message_text(text, reply_markup=keyboard)
    return False


async def chess_square_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    game_id, sq = int(parts[2]), int(parts[3])
    game = get_game(game_id)
    if game is None or game["status"] != "active":
        await query.answer("این بازی دیگه فعال نیست.", show_alert=True)
        return

    state = json.loads(game["state_json"])
    user = update.effective_user
    turn_color = state["turn"]
    turn_player_id = game["white_id"] if turn_color == "w" else game["black_id"]
    if user.id != turn_player_id:
        await query.answer("الان نوبتِ تو نیست!", show_alert=True)
        return

    selected_sq = game["selected_square"]
    piece_at_sq = state["board"][sq]

    if selected_sq is None:
        if not piece_at_sq or piece_at_sq[0] != turn_color:
            await query.answer("این خونه مهره‌ی تو نیست.", show_alert=True)
            return
        save_game_state(game_id, state, selected_square=sq)
        await query.answer()
        text, keyboard = build_board_view(game_id)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if sq == selected_sq:
        save_game_state(game_id, state, selected_square=None)
        await query.answer()
        text, keyboard = build_board_view(game_id)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if piece_at_sq and piece_at_sq[0] == turn_color:
        save_game_state(game_id, state, selected_square=sq)
        await query.answer()
        text, keyboard = build_board_view(game_id)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    legal = cr.legal_moves_for_square(state, selected_sq)
    chosen = None
    for m in legal:
        if m["to"] == sq:
            if m.get("promotion") and m["promotion"] != "Q":
                continue  # پروموشن همیشه خودکار وزیر - بقیه‌ی گزینه‌ها رو نادیده می‌گیریم
            chosen = m
            break
    if chosen is None:
        await query.answer("این حرکت قانونی نیست.", show_alert=True)
        return

    await query.answer()
    new_state = cr.apply_move(state, chosen)
    save_game_state(game_id, new_state, selected_square=None)
    finished = await _maybe_finish_and_render(query, game_id)

    if not finished and game["vs_bot"]:
        game2 = get_game(game_id)
        state2 = json.loads(game2["state_json"])
        if state2["turn"] == "b":  # نوبتِ ربات (همیشه سیاه)
            params = DIFFICULTY_PARAMS.get(game2["difficulty"], DIFFICULTY_PARAMS["medium"])
            bot_move = cr.best_move(state2, params["depth"], params["randomness"])
            if bot_move:
                state3 = cr.apply_move(state2, bot_move)
                save_game_state(game_id, state3, selected_square=None)
                game3 = get_game(game_id)
                state3_check = json.loads(game3["state_json"])
                status3 = cr.game_status(state3_check)
                if status3 != "ongoing":
                    result_text = finalize_game(game3, state3_check, status3)
                    text, keyboard = build_board_view(game_id)
                    await query.edit_message_text(text + "\n" + result_text, reply_markup=keyboard)
                else:
                    text, keyboard = build_board_view(game_id)
                    await query.edit_message_text(text, reply_markup=keyboard)


async def chess_rematch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    old_id = int(query.data.split("_")[-1])
    old = get_game(old_id)
    if old is None:
        return
    if old["vs_bot"]:
        new_id = create_game(old["chat_id"], old["white_id"], old["white_name"], 0, "ربات", True, old["difficulty"])
    else:
        # نفرِ اولِ دفعه‌ی قبل، این‌بار سیاه می‌شه - منصفانه‌تره
        new_id = create_game(old["chat_id"], old["black_id"], old["black_name"], old["white_id"], old["white_name"], False, None)
    text, keyboard = build_board_view(new_id)
    await query.edit_message_text(text, reply_markup=keyboard)
    set_message_id(new_id, query.message.message_id)


async def chess_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game_id = int(query.data.split("_")[-1])
    game = get_game(game_id)
    user_id = update.effective_user.id
    if game is not None and user_id not in {game["white_id"], game["black_id"]}:
        await query.answer("این بازی مال تو نیست، فقط بازیکن‌ها می‌تونن ببندنش.", show_alert=True)
        return
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        await query.edit_message_text("بازی بسته شد. ✅")


async def chess_bump_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game_id = int(query.data.split("_")[-1])
    game = get_game(game_id)
    if game is None or game["status"] != "active":
        await query.answer("این بازی دیگه فعال نیست.", show_alert=True)
        return
    user_id = update.effective_user.id
    if user_id not in {game["white_id"], game["black_id"]}:
        await query.answer("این بازی مال تو نیست، فقط بازیکن‌ها می‌تونن پایینش بیارن.", show_alert=True)
        return
    await query.answer()
    text, keyboard = build_board_view(game_id)
    try:
        sent = await context.bot.send_message(update.effective_chat.id, text, reply_markup=keyboard)
    except Exception:
        return
    set_message_id(game_id, sent.message_id)
    try:
        await query.message.delete()
    except Exception:
        pass


async def chess_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    stats = get_player_stats(user.id)
    lines = [f"👤 پروفایلِ شطرنجِ {user.first_name}", f"آی‌دیِ عددی: {user.id}", ""]
    if stats is None:
        lines.append("هنوز هیچ بازی‌ای نکردی.")
    else:
        lines.append(f"کل بازی‌ها: {stats['total_games']}")
        lines.append(f"برد: {stats['wins']} 🏆 | باخت: {stats['losses']} ❌ | مساوی: {stats['draws']} 🤝")
        u_title = get_user_title(stats["user_wins"])
        b_title = get_bot_title(stats["bot_wins"])
        if u_title:
            lines.append(f"لقبِ برابرِ کاربرها: {u_title}")
        if b_title:
            lines.append(f"لقبِ برابرِ ربات: {b_title}")

        opponents = get_opponents_list(user.id)
        if opponents:
            lines.append("\n📊 رودررو با بقیه:")
            for opp in opponents[:15]:
                lines.append(f"• {opp['name']}: {opp['wins']} برد / {opp['losses']} باخت / {opp['draws']} مساوی")

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="chess_back_mode")]])
    await query.edit_message_text("\n".join(lines), reply_markup=keyboard)


async def chess_back_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("♟️ شطرنج! چطوری بازی کنیم؟", reply_markup=mode_menu_keyboard())


# ---------------------------------------------------------------------------
# ثبت هندلرها
# ---------------------------------------------------------------------------
def register(app: Application):
    app.add_handler(CallbackQueryHandler(chess_mode_callback, pattern="^chess_mode_(user|bot)$"))
    app.add_handler(CallbackQueryHandler(chess_diff_callback, pattern="^chess_diff_"))
    app.add_handler(CallbackQueryHandler(chess_square_callback, pattern="^chess_sq_"))
    app.add_handler(CallbackQueryHandler(chess_rematch_callback, pattern="^chess_rematch_"))
    app.add_handler(CallbackQueryHandler(chess_close_callback, pattern="^chess_close_"))
    app.add_handler(CallbackQueryHandler(chess_bump_callback, pattern="^chess_bump_"))
    app.add_handler(CallbackQueryHandler(chess_profile_callback, pattern="^chess_profile$"))
    app.add_handler(CallbackQueryHandler(chess_back_mode_callback, pattern="^chess_back_mode$"))
