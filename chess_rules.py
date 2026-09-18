# -*- coding: utf-8 -*-
"""
موتور قوانینِ شطرنج - کاملاً مستقل از تلگرام، فقط منطقِ بازی.
========================================================================
نمایشِ صفحه: لیستی با ۶۴ خونه (ایندکس ۰ تا ۶۳). ایندکس = rank*8 + file
(rank از ۰ تا ۷ یعنی ردیف‌های ۱ تا ۸، file از ۰ تا ۷ یعنی ستون‌های a تا h).
هر خونه یا "" (خالی) یا یه کدِ ۲ کاراکتری: رنگ ('w'/'b') + نوع مهره
('P','N','B','R','Q','K'). مثلاً "wK" = شاهِ سفید.
"""
import copy

FILES = "abcdefgh"

PIECE_UNICODE = {
    "wK": "♔", "wQ": "♕", "wR": "♖", "wB": "♗", "wN": "♘", "wP": "♙",
    "bK": "♚", "bQ": "♛", "bR": "♜", "bB": "♝", "bN": "♞", "bP": "♟",
}

PIECE_VALUE = {"P": 100, "N": 320, "B": 330, "R": 500, "Q": 900, "K": 20000}

# جدول‌های پوزیشنی ساده (فقط برای این‌که هوشِ مصنوعی کمی «فهم موقعیتی» داشته باشه)
_PAWN_TABLE = [
    0, 0, 0, 0, 0, 0, 0, 0,
    5, 10, 10, -20, -20, 10, 10, 5,
    5, -5, -10, 0, 0, -10, -5, 5,
    0, 0, 0, 20, 20, 0, 0, 0,
    5, 5, 10, 25, 25, 10, 5, 5,
    10, 10, 20, 30, 30, 20, 10, 10,
    50, 50, 50, 50, 50, 50, 50, 50,
    0, 0, 0, 0, 0, 0, 0, 0,
]
_KNIGHT_TABLE = [
    -50, -40, -30, -30, -30, -30, -40, -50,
    -40, -20, 0, 5, 5, 0, -20, -40,
    -30, 5, 10, 15, 15, 10, 5, -30,
    -30, 0, 15, 20, 20, 15, 0, -30,
    -30, 5, 15, 20, 20, 15, 5, -30,
    -30, 0, 10, 15, 15, 10, 0, -30,
    -40, -20, 0, 0, 0, 0, -20, -40,
    -50, -40, -30, -30, -30, -30, -40, -50,
]
_POS_TABLES = {"P": _PAWN_TABLE, "N": _KNIGHT_TABLE}


def square_name(idx: int) -> str:
    return f"{FILES[idx % 8]}{idx // 8 + 1}"


def parse_square(name: str) -> int:
    return "abcdefgh".index(name[0]) + (int(name[1]) - 1) * 8


def initial_board():
    board = [""] * 64
    back_rank = ["R", "N", "B", "Q", "K", "B", "N", "R"]
    for f in range(8):
        board[f] = "w" + back_rank[f]
        board[8 + f] = "wP"
        board[48 + f] = "bP"
        board[56 + f] = "b" + back_rank[f]
    return board


def new_game_state():
    return {
        "board": initial_board(),
        "turn": "w",
        "castling": {"wK": True, "wQ": True, "bK": True, "bQ": True},
        "en_passant": None,  # ایندکسِ خونه‌ای که می‌شه آن‌پاسان توش گرفت، یا None
        "halfmove_clock": 0,  # برای قانونِ ۵۰ حرکت (بدونِ پیاده/گرفتنِ مهره)
        "history": [],  # لیستی از FEN-مانند برای تشخیصِ تکرارِ سه‌باره (ساده‌شده)
    }


def _color_of(piece: str) -> str:
    return piece[0] if piece else ""


def _in_bounds(rank: int, file: int) -> bool:
    return 0 <= rank < 8 and 0 <= file < 8


def _king_square(board, color: str):
    target = color + "K"
    for i, p in enumerate(board):
        if p == target:
            return i
    return None


# ---------------------------------------------------------------------------
# تولیدِ حرکت‌های شبه‌قانونی (بدونِ چک‌کردنِ این‌که شاهِ خودی کیش می‌مونه یا نه)
# ---------------------------------------------------------------------------
_KNIGHT_OFFSETS = [(1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)]
_KING_OFFSETS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
_BISHOP_DIRS = [(1, 1), (1, -1), (-1, 1), (-1, -1)]
_ROOK_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def _sliding_moves(board, sq, dirs, color):
    rank, file = sq // 8, sq % 8
    moves = []
    for dr, df in dirs:
        r, f = rank + dr, file + df
        while _in_bounds(r, f):
            target = r * 8 + f
            if board[target] == "":
                moves.append(target)
            else:
                if _color_of(board[target]) != color:
                    moves.append(target)
                break
            r, f = r + dr, f + df
    return moves


def _pawn_pseudo_moves(state, sq):
    board = state["board"]
    color = _color_of(board[sq])
    rank, file = sq // 8, sq % 8
    direction = 1 if color == "w" else -1
    start_rank = 1 if color == "w" else 6
    moves = []  # لیستی از (to_sq, is_capture, is_en_passant, promotes)
    one_step = (rank + direction) * 8 + file
    if _in_bounds(rank + direction, file) and board[one_step] == "":
        promotes = (rank + direction) in (0, 7)
        moves.append((one_step, False, False, promotes))
        if rank == start_rank:
            two_step = (rank + 2 * direction) * 8 + file
            if board[two_step] == "":
                moves.append((two_step, False, False, False))
    for df in (-1, 1):
        nf = file + df
        if not _in_bounds(rank + direction, nf):
            continue
        target = (rank + direction) * 8 + nf
        if board[target] != "" and _color_of(board[target]) != color:
            promotes = (rank + direction) in (0, 7)
            moves.append((target, True, False, promotes))
        elif state["en_passant"] == target:
            moves.append((target, True, True, False))
    return moves


def pseudo_legal_moves(state, sq):
    """همه‌ی حرکت‌های شبه‌قانونیِ مهره‌ی رویِ خونه‌ی sq. برمی‌گردونه لیستی از دیکشنری‌های حرکت."""
    board = state["board"]
    piece = board[sq]
    if not piece:
        return []
    color, kind = piece[0], piece[1]
    result = []

    if kind == "P":
        for to_sq, is_capture, is_ep, promotes in _pawn_pseudo_moves(state, sq):
            if promotes:
                for promo in ("Q", "R", "B", "N"):
                    result.append({"from": sq, "to": to_sq, "capture": is_capture, "en_passant": is_ep, "promotion": promo})
            else:
                result.append({"from": sq, "to": to_sq, "capture": is_capture, "en_passant": is_ep, "promotion": None})
        return result

    if kind == "N":
        rank, file = sq // 8, sq % 8
        for dr, df in _KNIGHT_OFFSETS:
            r, f = rank + dr, file + df
            if _in_bounds(r, f):
                target = r * 8 + f
                if board[target] == "" or _color_of(board[target]) != color:
                    result.append({"from": sq, "to": target, "capture": board[target] != "", "en_passant": False, "promotion": None})
        return result

    if kind == "B":
        for target in _sliding_moves(board, sq, _BISHOP_DIRS, color):
            result.append({"from": sq, "to": target, "capture": board[target] != "", "en_passant": False, "promotion": None})
        return result

    if kind == "R":
        for target in _sliding_moves(board, sq, _ROOK_DIRS, color):
            result.append({"from": sq, "to": target, "capture": board[target] != "", "en_passant": False, "promotion": None})
        return result

    if kind == "Q":
        for target in _sliding_moves(board, sq, _BISHOP_DIRS + _ROOK_DIRS, color):
            result.append({"from": sq, "to": target, "capture": board[target] != "", "en_passant": False, "promotion": None})
        return result

    if kind == "K":
        rank, file = sq // 8, sq % 8
        for dr, df in _KING_OFFSETS:
            r, f = rank + dr, file + df
            if _in_bounds(r, f):
                target = r * 8 + f
                if board[target] == "" or _color_of(board[target]) != color:
                    result.append({"from": sq, "to": target, "capture": board[target] != "", "en_passant": False, "promotion": None, "castle": None})
        # قلعه‌روی (castling) - شرط‌های اولیه؛ شرطِ «کیش نبودن در طول مسیر» جدا چک می‌شه
        home_rank = 0 if color == "w" else 7
        if sq == home_rank * 8 + 4:
            rights = state["castling"]
            if rights.get(color + "K") and board[home_rank * 8 + 5] == "" and board[home_rank * 8 + 6] == "":
                if board[home_rank * 8 + 7] == color + "R":
                    result.append({"from": sq, "to": home_rank * 8 + 6, "capture": False, "en_passant": False, "promotion": None, "castle": "K"})
            if rights.get(color + "Q") and board[home_rank * 8 + 3] == "" and board[home_rank * 8 + 2] == "" and board[home_rank * 8 + 1] == "":
                if board[home_rank * 8 + 0] == color + "R":
                    result.append({"from": sq, "to": home_rank * 8 + 2, "capture": False, "en_passant": False, "promotion": None, "castle": "Q"})
        return result

    return result


def is_square_attacked(board, sq, by_color):
    """آیا خونه‌ی sq توسط رنگِ by_color تهدید می‌شه؟ (برای تشخیصِ کیش و امنیتِ شاه)."""
    rank, file = sq // 8, sq % 8

    # پیاده: پیاده‌ی رنگِ حریف از کدوم جهت به این خونه حمله می‌کنه
    pawn_dir = -1 if by_color == "w" else 1  # پیاده‌ی سفید از پایین به بالا حمله می‌کنه؛ یعنی برعکسِ جهتِ حرکتش رو نگاه می‌کنیم
    for df in (-1, 1):
        r, f = rank + pawn_dir, file + df
        if _in_bounds(r, f) and board[r * 8 + f] == by_color + "P":
            return True

    for dr, df in _KNIGHT_OFFSETS:
        r, f = rank + dr, file + df
        if _in_bounds(r, f) and board[r * 8 + f] == by_color + "N":
            return True

    for dr, df in _KING_OFFSETS:
        r, f = rank + dr, file + df
        if _in_bounds(r, f) and board[r * 8 + f] == by_color + "K":
            return True

    for dr, df in _BISHOP_DIRS:
        r, f = rank + dr, file + df
        while _in_bounds(r, f):
            p = board[r * 8 + f]
            if p:
                if _color_of(p) == by_color and p[1] in ("B", "Q"):
                    return True
                break
            r, f = r + dr, f + df

    for dr, df in _ROOK_DIRS:
        r, f = rank + dr, file + df
        while _in_bounds(r, f):
            p = board[r * 8 + f]
            if p:
                if _color_of(p) == by_color and p[1] in ("R", "Q"):
                    return True
                break
            r, f = r + dr, f + df

    return False


def apply_move(state, move):
    """حرکت رو رویِ یه کپیِ جدید از وضعیت اعمال می‌کنه و همون وضعیتِ جدید رو برمی‌گردونه."""
    new_state = {
        "board": state["board"][:],
        "turn": "b" if state["turn"] == "w" else "w",
        "castling": dict(state["castling"]),
        "en_passant": None,
        "halfmove_clock": state["halfmove_clock"] + 1,
        "history": state["history"][:],
    }
    board = new_state["board"]
    frm, to = move["from"], move["to"]
    piece = board[frm]
    color, kind = piece[0], piece[1]

    if move.get("capture") or kind == "P":
        new_state["halfmove_clock"] = 0

    if move.get("en_passant"):
        captured_sq = to - 8 if color == "w" else to + 8
        board[captured_sq] = ""

    board[to] = (color + move["promotion"]) if move.get("promotion") else piece
    board[frm] = ""

    if move.get("castle") == "K":
        home_rank = 0 if color == "w" else 7
        board[home_rank * 8 + 5] = board[home_rank * 8 + 7]
        board[home_rank * 8 + 7] = ""
    elif move.get("castle") == "Q":
        home_rank = 0 if color == "w" else 7
        board[home_rank * 8 + 3] = board[home_rank * 8 + 0]
        board[home_rank * 8 + 0] = ""

    if kind == "K":
        new_state["castling"][color + "K"] = False
        new_state["castling"][color + "Q"] = False
    if kind == "R":
        if frm == (0 if color == "w" else 56):
            new_state["castling"][color + "Q"] = False
        elif frm == (7 if color == "w" else 63):
            new_state["castling"][color + "K"] = False
    # اگه یه رخ گرفته بشه، حقِ قلعه‌روی از طرفِ صاحبِ اون رخ هم از بین می‌ره
    for c in ("w", "b"):
        if board[0 if c == "w" else 56] != c + "R":
            new_state["castling"][c + "Q"] = False
        if board[7 if c == "w" else 63] != c + "R":
            new_state["castling"][c + "K"] = False

    if kind == "P" and abs(to - frm) == 16:
        new_state["en_passant"] = (frm + to) // 2

    new_state["history"].append(board_signature(new_state))
    return new_state


def board_signature(state) -> str:
    """یه امضای ساده از وضعیتِ فعلی (برای تشخیصِ تکرارِ سه‌باره)."""
    return "".join(p or "." for p in state["board"]) + state["turn"] + str(state["castling"]) + str(state["en_passant"])


def legal_moves_for_square(state, sq):
    color = state["turn"]
    if not state["board"][sq] or _color_of(state["board"][sq]) != color:
        return []
    legal = []
    for move in pseudo_legal_moves(state, sq):
        if move.get("castle"):
            home_rank = 0 if color == "w" else 7
            king_sq = home_rank * 8 + 4
            if is_square_attacked(state["board"], king_sq, "b" if color == "w" else "w"):
                continue
            path = [home_rank * 8 + 5, home_rank * 8 + 6] if move["castle"] == "K" else [home_rank * 8 + 3, home_rank * 8 + 2]
            if any(is_square_attacked(state["board"], sq2, "b" if color == "w" else "w") for sq2 in path):
                continue
        new_state = apply_move(state, move)
        king_sq = _king_square(new_state["board"], color)
        if not is_square_attacked(new_state["board"], king_sq, "b" if color == "w" else "w"):
            legal.append(move)
    return legal


def all_legal_moves(state):
    color = state["turn"]
    moves = []
    for sq in range(64):
        if state["board"][sq] and _color_of(state["board"][sq]) == color:
            moves.extend(legal_moves_for_square(state, sq))
    return moves


def is_in_check(state, color=None) -> bool:
    color = color or state["turn"]
    king_sq = _king_square(state["board"], color)
    if king_sq is None:
        return False
    return is_square_attacked(state["board"], king_sq, "b" if color == "w" else "w")


def game_status(state) -> str:
    """برمی‌گردونه: 'ongoing' | 'checkmate' | 'stalemate' | 'draw_50move' | 'draw_repetition' | 'draw_material'."""
    moves = all_legal_moves(state)
    if not moves:
        return "checkmate" if is_in_check(state) else "stalemate"
    if state["halfmove_clock"] >= 100:  # ۵۰ حرکتِ کامل = ۱۰۰ نیم‌حرکت
        return "draw_50move"
    if state["history"].count(state["history"][-1]) >= 3 if state["history"] else False:
        return "draw_repetition"
    if _insufficient_material(state["board"]):
        return "draw_material"
    return "ongoing"


def _insufficient_material(board) -> bool:
    pieces = [p for p in board if p]
    if len(pieces) <= 2:  # فقط دو شاه
        return True
    if len(pieces) == 3 and any(p[1] in ("N", "B") for p in pieces):
        return True  # شاه+شاه+یه اسب یا فیل
    return False


# ---------------------------------------------------------------------------
# ارزیابیِ موقعیت + هوشِ مصنوعی (مینی‌مکس با هرسِ آلفا-بتا)
# ---------------------------------------------------------------------------
def evaluate(state) -> int:
    """امتیازِ موقعیت از دیدِ سفید (مثبت = بهترِ سفید، منفی = بهترِ سیاه)."""
    score = 0
    for sq, p in enumerate(state["board"]):
        if not p:
            continue
        color, kind = p[0], p[1]
        val = PIECE_VALUE[kind]
        table = _POS_TABLES.get(kind)
        if table:
            idx = sq if color == "w" else (63 - sq)
            val += table[idx]
        score += val if color == "w" else -val
    return score


def _order_moves(moves):
    return sorted(moves, key=lambda m: (0 if m.get("capture") else 1, 0 if m.get("promotion") == "Q" else 1))


def _minimax(state, depth, alpha, beta, maximizing):
    status = game_status(state)
    if status != "ongoing":
        if status == "checkmate":
            # هرکی نوبتشه و کیش‌ومات شده، بازنده‌ست
            return (-99000 + (5 - depth)) if state["turn"] == "w" else (99000 - (5 - depth))
        return 0  # مساوی

    if depth == 0:
        return evaluate(state)

    moves = _order_moves(all_legal_moves(state))
    if maximizing:
        best = float("-inf")
        for m in moves:
            best = max(best, _minimax(apply_move(state, m), depth - 1, alpha, beta, False))
            alpha = max(alpha, best)
            if beta <= alpha:
                break
        return best
    else:
        best = float("inf")
        for m in moves:
            best = min(best, _minimax(apply_move(state, m), depth - 1, alpha, beta, True))
            beta = min(beta, best)
            if beta <= alpha:
                break
        return best


def best_move(state, depth: int, randomness: float = 0.0):
    """بهترین حرکت رو از دیدِ رنگِ نوبت‌دار پیدا می‌کنه. randomness (۰ تا ۱) یعنی چقدر
    ممکنه به‌جای بهترین حرکت، یکی از حرکت‌های نه‌چندان‌بدِ دیگه رو به‌صورتِ تصادفی بزنه
    (برای درجه‌ی سختیِ آسون، تا بازی قابل‌شکست‌تر بشه)."""
    import random

    color = state["turn"]
    maximizing = color == "w"
    moves = _order_moves(all_legal_moves(state))
    if not moves:
        return None
    scored = []
    alpha, beta = float("-inf"), float("inf")
    for m in moves:
        score = _minimax(apply_move(state, m), depth - 1, alpha, beta, not maximizing)
        scored.append((score, m))
        if maximizing:
            alpha = max(alpha, score)
        else:
            beta = min(beta, score)
    scored.sort(key=lambda x: x[0], reverse=maximizing)

    if randomness > 0 and len(scored) > 1:
        pool_size = max(1, int(len(scored) * randomness))
        pick_from = scored[:max(pool_size, 3)] if len(scored) >= 3 else scored
        return random.choice(pick_from)[1]
    return scored[0][1]
