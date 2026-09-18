# -*- coding: utf-8 -*-
"""
بک‌اندِ واقعیِ پنلِ بازی‌های کولیبا
=====================================
این یه سرویسِ کاملاً جداست - باید جدا از خودِ ربات (به‌عنوانِ یه سرویسِ دومِ Railway، تو همون
پروژه) دیپلوی بشه، ولی از همون دیتابیسِ ربات (متغیرِ محیطیِ DATABASE_URL) استفاده می‌کنه.

Environment variables لازم:
    BOT_TOKEN        - همون توکنِ ربات (برای فرستادنِ پیام به گروه‌ها + چک‌کردنِ initData)
    BOT_USERNAME      - یوزرنیمِ ربات بدونِ @ (مثلاً KolibaBot)
    DATABASE_URL      - همون کانکشن‌استرینگِ Postgres که خودِ ربات استفاده می‌کنه

اجرا: gunicorn webapp_backend:app   (یا برای تستِ محلی: python webapp_backend.py)
"""

import os
import json
import random
import string
import hashlib
import hmac
import asyncio
from datetime import datetime
from urllib.parse import unquote

from flask import Flask, request, jsonify
from flask_cors import CORS

import pg_compat  # همون فایلِ pg_compat.py که کنارِ bot-23.py هست؛ باید تو همین ریپازیتوری باشه

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@")

app = Flask(__name__)
CORS(app)  # چون فرانت‌اند از یه دامنه‌ی دیگه (مثلاً GitHub Pages) صدا زده می‌شه

DEFAULT_KOLICOIN = 20
WEBAPP_XP_PER_LEVEL = 200
WEBAPP_MAX_LEVEL = 50
WEBAPP_WIN_XP = 20
WEBAPP_LOSS_XP = -5

# ---- ماموریت‌ها: پاداشِ یک‌بارمصرف، جدا از سکه‌ی پیش‌فرضِ ۲۰ تایی ----------
MISSION_JOIN_REWARD = 1
MISSION_5GAMES_REWARD = 5
MISSION_10GAMES_REWARD = 15


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
def get_conn():
    pg_url = pg_compat.postgres_url()
    if not pg_url:
        raise RuntimeError("DATABASE_URL تنظیم نشده؛ این بک‌اند فقط با همون PostgreSQLِ ربات کار می‌کنه.")
    return pg_compat.connect(pg_url)


def init_webapp_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS webapp_users (
            user_id BIGINT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            kolicoin INTEGER NOT NULL DEFAULT 20,
            xp INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS webapp_games (
            game_id TEXT PRIMARY KEY,
            game_type TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'normal',
            bet_amount INTEGER NOT NULL DEFAULT 0,
            creator_id BIGINT NOT NULL,
            creator_name TEXT,
            opponent_id BIGINT,
            opponent_name TEXT,
            status TEXT NOT NULL DEFAULT 'waiting',
            state TEXT,
            turn BIGINT,
            winner_id BIGINT,
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS webapp_missions (
            user_id BIGINT PRIMARY KEY,
            claimed_join INTEGER NOT NULL DEFAULT 0,
            claimed_5games INTEGER NOT NULL DEFAULT 0,
            claimed_10games INTEGER NOT NULL DEFAULT 0,
            games_played INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


init_webapp_db()


# ---------------------------------------------------------------------------
# چکِ اصالتِ initData (طبقِ مستنداتِ رسمیِ تلگرام) - بدونِ این، هرکسی می‌تونست به‌جای هرکسی
# درخواست بفرسته. این تابع مطمئن می‌شه دیتا واقعاً از طرفِ تلگرام اومده و دست‌کاری نشده.
# ---------------------------------------------------------------------------
def verify_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = [p.split("=", 1) for p in init_data.split("&") if "=" in p]
        parsed = {k: v for k, v in pairs}
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={unquote(v)}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        user = json.loads(unquote(parsed.get("user", "{}")))
        if "id" not in user:
            return None
        return user
    except Exception:
        return None


def require_user():
    """از بدنه‌ی JSONِ درخواست، initData رو می‌گیره و چک می‌کنه؛ کاربرِ واقعی یا None برمی‌گردونه."""
    data = request.get_json(silent=True) or {}
    return verify_init_data(data.get("initData", "")), data


# ---------------------------------------------------------------------------
# کاربرها
# ---------------------------------------------------------------------------
def ensure_webapp_user(user_id: int, first_name: str = "", username: str = ""):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM webapp_users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    is_new = row is None
    if row is None:
        # تاریخِ آشناییِ واقعیِ این کاربر با ربات رو از دیتابیسِ خودِ ربات پیدا می‌کنیم (اگه باشه)
        c.execute("SELECT MIN(first_seen) AS fs FROM group_members WHERE user_id=?", (user_id,))
        fs_row = c.fetchone()
        joined_at = (fs_row["fs"] if fs_row and fs_row["fs"] else datetime.utcnow().isoformat())
        c.execute(
            "INSERT INTO webapp_users (user_id, first_name, username, kolicoin, xp, wins, losses, joined_at) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, ?)",
            (user_id, first_name, username, DEFAULT_KOLICOIN, joined_at),
        )
        conn.commit()
        c.execute("SELECT * FROM webapp_users WHERE user_id=?", (user_id,))
        row = c.fetchone()
    elif first_name or username:
        c.execute("UPDATE webapp_users SET first_name=?, username=? WHERE user_id=?", (first_name, username, user_id))
        conn.commit()
    conn.close()
    if is_new:
        claim_join_mission(user_id)
        conn2 = get_conn()
        c2 = conn2.cursor()
        c2.execute("SELECT * FROM webapp_users WHERE user_id=?", (user_id,))
        row = c2.fetchone()
        conn2.close()
    return row


# ---------------------------------------------------------------------------
# ماموریت‌ها
# ---------------------------------------------------------------------------
def _get_or_create_mission_row(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM webapp_missions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO webapp_missions (user_id, claimed_join, claimed_5games, claimed_10games, games_played) "
            "VALUES (?, 0, 0, 0, 0)",
            (user_id,),
        )
        conn.commit()
        c.execute("SELECT * FROM webapp_missions WHERE user_id=?", (user_id,))
        row = c.fetchone()
    conn.close()
    return row


def claim_join_mission(user_id: int):
    """ماموریتِ «ورود»: همون بارِ اولی که کاربر تو پنل دیده می‌شه، یک‌بار برای همیشه ۱ کولی‌کوین می‌گیره."""
    row = _get_or_create_mission_row(user_id)
    if row["claimed_join"]:
        return False
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE webapp_missions SET claimed_join=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    set_kolicoin(user_id, MISSION_JOIN_REWARD)
    return True


def increment_games_played_and_claim_missions(user_id: int):
    """بعدِ هر بازیِ تموم‌شده (چه برد چه باخت) صدا زده می‌شه: شمارشِ بازی‌ها رو زیاد می‌کنه و
    اگه به ۵ یا ۱۰ رسیده بود، پاداشِ همون ماموریت رو (فقط یک‌بار) می‌ده.
    برمی‌گردونه لیستی از (اسمِ ماموریت, مقدارِ پاداش) که تازه گرفته شده."""
    row = _get_or_create_mission_row(user_id)
    games_played = row["games_played"] + 1
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE webapp_missions SET games_played=? WHERE user_id=?", (games_played, user_id))
    conn.commit()
    conn.close()

    newly_claimed = []
    if games_played >= 5 and not row["claimed_5games"]:
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE webapp_missions SET claimed_5games=1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        set_kolicoin(user_id, MISSION_5GAMES_REWARD)
        newly_claimed.append(("۵ بازی", MISSION_5GAMES_REWARD))
    if games_played >= 10 and not row["claimed_10games"]:
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE webapp_missions SET claimed_10games=1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()
        set_kolicoin(user_id, MISSION_10GAMES_REWARD)
        newly_claimed.append(("۱۰ بازی", MISSION_10GAMES_REWARD))
    return newly_claimed


def get_missions_status(user_id: int):
    row = _get_or_create_mission_row(user_id)
    return {
        "join": {"reward": MISSION_JOIN_REWARD, "claimed": bool(row["claimed_join"])},
        "five_games": {
            "reward": MISSION_5GAMES_REWARD,
            "claimed": bool(row["claimed_5games"]),
            "progress": min(row["games_played"], 5),
            "target": 5,
        },
        "ten_games": {
            "reward": MISSION_10GAMES_REWARD,
            "claimed": bool(row["claimed_10games"]),
            "progress": min(row["games_played"], 10),
            "target": 10,
        },
    }



    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT kolicoin FROM webapp_users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    current = row["kolicoin"] if row else DEFAULT_KOLICOIN
    new_val = max(0, current + delta)
    c.execute("UPDATE webapp_users SET kolicoin=? WHERE user_id=?", (new_val, user_id))
    conn.commit()
    conn.close()
    return new_val


def add_webapp_result(user_id: int, won: bool):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT xp, wins, losses FROM webapp_users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    xp = row["xp"] if row else 0
    wins = row["wins"] if row else 0
    losses = row["losses"] if row else 0
    xp = max(0, xp + (WEBAPP_WIN_XP if won else WEBAPP_LOSS_XP))
    if won:
        wins += 1
    else:
        losses += 1
    c.execute("UPDATE webapp_users SET xp=?, wins=?, losses=? WHERE user_id=?", (xp, wins, losses, user_id))
    conn.commit()
    conn.close()


def get_bot_level_info(user_id: int):
    """لولِ اصلیِ خودِ ربات (چالش‌های ریاضی) و سطحِ ریاضی رو از همون جدول‌های ربات می‌خونیم -
    فرمول‌ها عیناً از bot-23.py کپی شدن (level_from_xp و subject_level_from_count)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT xp FROM user_levels WHERE user_id=?", (user_id,))
    xp_row = c.fetchone()
    xp = xp_row["xp"] if xp_row else 0
    c.execute("SELECT correct_count FROM user_subject_levels WHERE user_id=? AND subject=?", (user_id, "ریاضی"))
    math_row = c.fetchone()
    math_count = math_row["correct_count"] if math_row else 0
    conn.close()
    bot_level = 50 if xp >= 5000 else (1 + xp // 100)
    math_level = min(10, 1 + math_count // 10)
    return bot_level, math_count, math_level


def webapp_level_info(xp: int):
    level = min(WEBAPP_MAX_LEVEL, 1 + xp // WEBAPP_XP_PER_LEVEL)
    pct = 100 if level >= WEBAPP_MAX_LEVEL else round(((xp % WEBAPP_XP_PER_LEVEL) / WEBAPP_XP_PER_LEVEL) * 100)
    return level, pct


@app.route("/api/auth", methods=["POST"])
def api_auth():
    user, _ = require_user()
    if not user:
        return jsonify({"error": "invalid_init_data"}), 401
    row = ensure_webapp_user(user["id"], user.get("first_name", ""), user.get("username", ""))
    bot_level, math_count, math_level = get_bot_level_info(user["id"])
    webapp_level, webapp_pct = webapp_level_info(row["xp"])
    missions = get_missions_status(user["id"])
    return jsonify(
        {
            "user_id": row["user_id"],
            "first_name": row["first_name"],
            "username": row["username"],
            "joined_at": row["joined_at"],
            "kolicoin": row["kolicoin"],
            "xp": row["xp"],
            "level": webapp_level,
            "level_pct": webapp_pct,
            "wins": row["wins"],
            "losses": row["losses"],
            "bot_level": bot_level,
            "math_correct_count": math_count,
            "math_level": math_level,
            "missions": missions,
        }
    )


# ---------------------------------------------------------------------------
# فرستادنِ پیام به گروه‌ها (برای بازیِ رندوم) - مستقیم از پایتون-تلگرام-بات
# ---------------------------------------------------------------------------
def _broadcast_random_game(user, game_type: str, mode: str, bet_amount: int, invite_link: str):
    """برمی‌گردونه (sent_count, total_count, last_error) تا بشه دقیقاً فهمید چرا به گروهی
    نرسیده - قبلاً این خطاها کاملاً بی‌صدا قورت داده می‌شدن، پس هیچ‌وقت معلوم نمی‌شد مشکل کجاست."""
    from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

    game_labels = {"dooz": "⭕❌ دوز", "chess": "♟️ شطرنج", "penalty": "⚽ پنالتی"}
    first_name = user.get("first_name") or "یکی از بچه‌ها"
    bet_line = f"🪙 شرط: {bet_amount} کولی‌کوین" if mode == "bet" else "🎲 بازیِ عادی (بدونِ شرط)"
    text = f"🎮 {first_name} یه درخواستِ بازیِ {game_labels.get(game_type, game_type)} فعلاً باز کرده!\n{bet_line}\n\nمی‌تونید از دکمه‌ی زیر قبول کنید 👇"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ قبول کردن", url=invite_link)]])

    async def _run():
        sent, last_error = 0, None
        async with Bot(token=BOT_TOKEN) as bot:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT chat_id FROM group_settings")
            chat_ids = [r["chat_id"] for r in c.fetchall()]
            conn.close()
            for chat_id in chat_ids:
                try:
                    await bot.send_message(chat_id, text, reply_markup=keyboard)
                    sent += 1
                except Exception as e:
                    last_error = str(e)
                    continue
        return sent, len(chat_ids), last_error

    return asyncio.run(_run())


def gen_game_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def initial_state_for(game_type: str):
    if game_type == "dooz":
        return {"cells": [""] * 9}
    return {"note": "این بازی هنوز به موتورِ واقعی وصل نشده"}


@app.route("/api/game/create", methods=["POST"])
def api_game_create():
    user, data = require_user()
    if not user:
        return jsonify({"error": "invalid_init_data"}), 401
    if not BOT_USERNAME:
        # این خیلی مهمه: اگه این ست نباشه، لینکِ دعوت خرابه (t.me/?startapp=...) و تلگرام
        # اصلاً نمی‌ذاره پیامِ دکمه‌دار فرستاده بشه - قبلاً این خطا کاملاً بی‌صدا بود.
        return jsonify({"error": "bot_username_not_configured"}), 500
    user_id = user["id"]
    game_type = data.get("game_type")
    mode = data.get("mode", "normal")
    target = data.get("target")  # 'friend' | 'random'
    bet_amount = int(data.get("bet_amount", 0)) if mode == "bet" else 0

    if game_type not in ("dooz", "chess", "penalty"):
        return jsonify({"error": "bad_game_type"}), 400
    if mode == "bet":
        row = ensure_webapp_user(user_id, user.get("first_name", ""), user.get("username", ""))
        if bet_amount <= 0 or row["kolicoin"] < bet_amount:
            return jsonify({"error": "not_enough_coins", "kolicoin": row["kolicoin"]}), 400

    game_id = gen_game_id()
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO webapp_games (game_id, game_type, mode, bet_amount, creator_id, creator_name, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?)",
        (game_id, game_type, mode, bet_amount, user_id, user.get("first_name", ""), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    invite_link = f"https://t.me/{BOT_USERNAME}?startapp={game_id}"
    broadcast_info = None

    if target == "random":
        try:
            sent, total, last_error = _broadcast_random_game(user, game_type, mode, bet_amount, invite_link)
            broadcast_info = {"sent": sent, "total": total, "last_error": last_error}
        except Exception as e:
            broadcast_info = {"sent": 0, "total": 0, "last_error": str(e)}

    return jsonify({"game_id": game_id, "invite_link": invite_link, "broadcast": broadcast_info})


@app.route("/api/game/join", methods=["POST"])
def api_game_join():
    user, data = require_user()
    if not user:
        return jsonify({"error": "invalid_init_data"}), 401
    user_id = user["id"]
    game_id = data.get("game_id")

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM webapp_games WHERE game_id=?", (game_id,))
    game = c.fetchone()
    if game is None:
        conn.close()
        return jsonify({"error": "not_found"}), 404
    if game["status"] != "waiting":
        conn.close()
        return jsonify({"error": "already_started", "status": game["status"]}), 400
    if game["creator_id"] == user_id:
        conn.close()
        return jsonify({"error": "cant_join_own_game"}), 400

    if game["mode"] == "bet":
        creator_row = ensure_webapp_user(game["creator_id"])
        opponent_row = ensure_webapp_user(user_id, user.get("first_name", ""), user.get("username", ""))
        if creator_row["kolicoin"] < game["bet_amount"] or opponent_row["kolicoin"] < game["bet_amount"]:
            conn.close()
            return jsonify({"error": "not_enough_coins"}), 400

    initial_state = json.dumps(initial_state_for(game["game_type"]))
    c.execute(
        "UPDATE webapp_games SET opponent_id=?, opponent_name=?, status='active', state=?, turn=? WHERE game_id=?",
        (user_id, user.get("first_name", ""), initial_state, game["creator_id"], game_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/game/<game_id>/state", methods=["GET"])
def api_game_state(game_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM webapp_games WHERE game_id=?", (game_id,))
    game = c.fetchone()
    conn.close()
    if game is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(
        {
            "game_id": game["game_id"],
            "game_type": game["game_type"],
            "mode": game["mode"],
            "bet_amount": game["bet_amount"],
            "creator_id": game["creator_id"],
            "creator_name": game["creator_name"],
            "opponent_id": game["opponent_id"],
            "opponent_name": game["opponent_name"],
            "status": game["status"],
            "state": json.loads(game["state"]) if game["state"] else {},
            "turn": game["turn"],
            "winner_id": game["winner_id"],
        }
    )


DOOZ_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def check_dooz_winner(cells):
    for a, b, c_ in DOOZ_LINES:
        if cells[a] and cells[a] == cells[b] == cells[c_]:
            return cells[a]
    if all(cells):
        return "draw"
    return None


def settle_finished_game(game, winner_id):
    """XPِ پنلِ بازی‌ها، کولی‌کوینِ شرط، و پیشرفتِ ماموریت‌های «۵ بازی»/«۱۰ بازی» رو تسویه
    می‌کنه (فقط وقتی برنده مشخصه، نه مساوی)."""
    if winner_id is None:
        return
    loser_id = game["opponent_id"] if winner_id == game["creator_id"] else game["creator_id"]
    add_webapp_result(winner_id, True)
    add_webapp_result(loser_id, False)
    if game["mode"] == "bet" and game["bet_amount"] > 0:
        set_kolicoin(winner_id, game["bet_amount"])
        set_kolicoin(loser_id, -game["bet_amount"])
    increment_games_played_and_claim_missions(winner_id)
    increment_games_played_and_claim_missions(loser_id)


@app.route("/api/game/<game_id>/move", methods=["POST"])
def api_game_move(game_id):
    user, data = require_user()
    if not user:
        return jsonify({"error": "invalid_init_data"}), 401
    user_id = user["id"]

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM webapp_games WHERE game_id=?", (game_id,))
    game = c.fetchone()
    if game is None or game["status"] != "active":
        conn.close()
        return jsonify({"error": "not_active"}), 400
    if game["turn"] != user_id:
        conn.close()
        return jsonify({"error": "not_your_turn"}), 400

    if game["game_type"] != "dooz":
        conn.close()
        return jsonify({"error": "game_not_implemented_yet"}), 400

    cell = int(data.get("cell", -1))
    st = json.loads(game["state"])
    if cell < 0 or cell > 8 or st["cells"][cell]:
        conn.close()
        return jsonify({"error": "bad_move"}), 400

    mark = "X" if user_id == game["creator_id"] else "O"
    st["cells"][cell] = mark
    result = check_dooz_winner(st["cells"])
    next_turn = game["opponent_id"] if user_id == game["creator_id"] else game["creator_id"]
    new_status, winner_id = "active", None
    if result == "draw":
        new_status = "finished"
    elif result:
        new_status = "finished"
        winner_id = user_id

    c.execute(
        "UPDATE webapp_games SET state=?, turn=?, status=?, winner_id=? WHERE game_id=?",
        (json.dumps(st), next_turn, new_status, winner_id, game_id),
    )
    conn.commit()
    conn.close()

    if new_status == "finished" and winner_id is not None:
        settle_finished_game(game, winner_id)

    return jsonify({"ok": True})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "koliba-webapp-backend"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
