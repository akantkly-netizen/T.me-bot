# -*- coding: utf-8 -*-
"""
هماهنگ‌کننده‌ی سبک بین بازی‌ها.
================================
چون هم دوز و هم سنگ‌کاغذقیچی از یه کلمه‌ی مشترک («بازی» به‌صورت ریپلای) برای شروعِ
چالش با یه کاربرِ دیگه استفاده می‌کنن، این‌جا فقط یادمون می‌مونه که هر کاربر آخرین بار
کدوم بازی/حالت رو از منو انتخاب کرده، تا وقتی ریپلای زد و «بازی» نوشت، بدونیم باید
کدوم بازی رو براش شروع کنیم.
"""

_PENDING = {}  # user_id -> (game_kind, extra)  مثلا ("dooz", "classic") یا ("rps", None)


def set_pending(user_id: int, kind: str, extra=None):
    _PENDING[user_id] = (kind, extra)


def pop_pending(user_id: int, default=("dooz", "limited")):
    return _PENDING.pop(user_id, default)
