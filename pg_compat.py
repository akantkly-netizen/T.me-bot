# -*- coding: utf-8 -*-
"""
لایه‌ی سازگاریِ SQLite <-> PostgreSQL
========================================
هدف: بیش از صد جای پخش‌شده‌ی c.execute(...) تو bot.py / dooz_game.py / rps_game.py که با
نحوِ SQLite نوشته شدن (علامتِ سوال به‌جای پارامتر، INSERT OR IGNORE، AUTOINCREMENT،
PRAGMA table_info، و ردیف‌هایی که هم با ایندکسِ عددی هم با اسمِ ستون قابل‌خوندنن) رو بدونِ
دست‌زدن به تک‌تکِ اونا، روی PostgreSQL هم درست اجرا کنیم.

این فایل هیچ دیتایی رو خودش جابه‌جا نمی‌کنه؛ فقط یه "مترجمِ" شفاف بینِ کدِ ربات و درایورِ
psycopg2ه. اگه env var مربوط به Postgres ست نباشه، هیچ‌کدوم از این کد اجرا نمی‌شه و ربات
دقیقاً مثلِ قبل با SQLite کار می‌کنه (بدونِ هیچ ریسکی برای نصب‌های فعلی).
"""
import os
import re

try:
    import psycopg2
    import psycopg2.extensions
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False


def postgres_url() -> str:
    """آدرسِ اتصال به Postgres رو از متغیرهای محیطیِ رایج (Railway/Heroku/عمومی) پیدا می‌کنه."""
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_PUBLIC_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    )


def postgres_enabled() -> bool:
    return bool(postgres_url())


# ---------------------------------------------------------------------------
# ردیف: هم با ایندکسِ عددی (row[0]) هم با اسمِ ستون (row['x']) قابل‌خوندن - دقیقاً مثلِ sqlite3.Row
# ---------------------------------------------------------------------------
class Row:
    __slots__ = ("_keys", "_map")

    def __init__(self, keys, values):
        self._keys = list(keys)
        self._map = dict(zip(self._keys, values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._map[self._keys[key]]
        return self._map[key]

    def __contains__(self, key):
        return key in self._map

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._map.values())

    def __len__(self):
        return len(self._keys)

    def __repr__(self):
        return f"<Row {self._map!r}>"


# ---------------------------------------------------------------------------
# ترجمه‌ی نحوِ SQLite -> PostgreSQL
# ---------------------------------------------------------------------------
_AUTOINCR_RE = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE)
_BARE_INTEGER_RE = re.compile(r"\bINTEGER\b", re.IGNORECASE)
_INSERT_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
_PRAGMA_TABLEINFO_RE = re.compile(r"PRAGMA\s+table_info\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.IGNORECASE)
_PRAGMA_ANY_RE = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)


def _translate_placeholders(sql: str) -> str:
    """علامتِ سوالِ SQLite (?) رو به %sِ psycopg2 تبدیل می‌کنه؛ داخلِ رشته‌های تحت‌اللفظیِ
    کوتیشن‌دار (که تو SQLِ این پروژه اصلاً پیش نمیاد) رو دست نمی‌زنه، برای احتیاطِ بیشتر."""
    out = []
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == "?" and not in_string:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def translate_sql(sql: str) -> str:
    sql = _AUTOINCR_RE.sub("BIGSERIAL PRIMARY KEY", sql)
    if _INSERT_IGNORE_RE.search(sql):
        sql = _INSERT_IGNORE_RE.sub("INSERT INTO", sql)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # نکته‌ی حیاتی: تو SQLite ستونِ INTEGER هر عددی (هر چقدرم بزرگ) رو جا می‌ده، ولی
    # INTEGER تو PostgreSQL فقط ۳۲ بیتیه (سقفش ~۲.۱ میلیارد). آی‌دیِ گروه‌های سوپرگروپِ
    # تلگرام (مثلاً -1001234567890) و آی‌دیِ کاربرهای جدید هر دو از این سقف رد می‌شن؛ برای
    # همین هر INTEGERِ باقی‌مونده رو به BIGINT (۶۴ بیتی) تبدیل می‌کنیم تا هیچ‌وقت سرریز نشه.
    sql = _BARE_INTEGER_RE.sub("BIGINT", sql)
    return _translate_placeholders(sql)


def split_script(script: str):
    """معادلِ executescript ساده: اسکریپت رو با ; می‌شکنه (فقط برای بلوک‌های CREATE TABLE
    که تو این پروژه استفاده می‌شن - هیچ‌کدوم شاملِ ; داخلِ رشته یا تابع نیستن)."""
    parts = [p.strip() for p in script.split(";")]
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# کِرسر
# ---------------------------------------------------------------------------
class PGCursor:
    def __init__(self, real_cursor):
        self._real = real_cursor
        self._pragma_rows = None  # فقط وقتی PRAGMA table_info صدا زده شده باشه پر می‌شه

    def _safe_rollback(self):
        """
        نکته‌ی حیاتی درباره‌ی Postgres (برخلافِ SQLite): وقتی یه کوئری تو یه تراکنش خطا
        بده، خودِ همون کانکشن «آلوده» می‌مونه و هر کوئریِ بعدی روش - حتی کاملاً بی‌ربط -
        هم با خطای «current transaction is aborted» شکست می‌خوره، تا وقتی صریحاً rollback
        بشه. چون ما یه کانکشنِ دائمی (persistent) داریم که هیچ‌وقت خودش بسته نمی‌شه، بدونِ
        این safety net، یه خطای کوچیک تو یه کوئری می‌تونست کلِ ربات رو تا ری‌استارتِ بعدی
        (رو همه‌ی گروه‌ها، برای هر دستوری) خراب نگه داره.
        """
        try:
            self._real.connection.rollback()
        except Exception:
            pass

    def execute(self, sql, params=()):
        m = _PRAGMA_TABLEINFO_RE.search(sql)
        if m:
            table_name = m.group(1)
            try:
                self._real.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s ORDER BY ordinal_position",
                    (table_name,),
                )
            except Exception:
                self._safe_rollback()
                raise
            names = [r[0] for r in self._real.fetchall()]
            # ترتیبِ ستون‌های pragma واقعیِ SQLite: cid, name, type, notnull, dflt_value, pk
            self._pragma_rows = [
                Row(["cid", "name", "type", "notnull", "dflt_value", "pk"], [i, name, "", 0, None, 0])
                for i, name in enumerate(names)
            ]
            return self
        self._pragma_rows = None
        if _PRAGMA_ANY_RE.match(sql):
            return self  # سایر PRAGMAها (WAL و ...) برای Postgres بی‌معنی و بی‌خطرن؛ نادیده گرفته می‌شن
        translated = translate_sql(sql)
        try:
            self._real.execute(translated, params if params else None)
        except Exception:
            self._safe_rollback()
            raise
        return self

    def executescript(self, script):
        for stmt in split_script(script):
            self.execute(stmt)

    def executemany(self, sql, seq_of_params):
        translated = translate_sql(sql)
        try:
            self._real.executemany(translated, list(seq_of_params))
        except Exception:
            self._safe_rollback()
            raise

    def _wrap(self, raw_row):
        if raw_row is None:
            return None
        keys = [d[0] for d in self._real.description]
        return Row(keys, raw_row)

    def fetchone(self):
        if self._pragma_rows is not None:
            return self._pragma_rows.pop(0) if self._pragma_rows else None
        return self._wrap(self._real.fetchone())

    def fetchall(self):
        if self._pragma_rows is not None:
            rows, self._pragma_rows = self._pragma_rows, []
            return rows
        return [self._wrap(r) for r in self._real.fetchall()]

    @property
    def lastrowid(self):
        """معادلِ psycopg2 برای lastrowid: آخرین مقدارِ سکانسِ SERIAL تو همین سشن."""
        try:
            self._real.execute("SELECT lastval()")
        except Exception:
            self._safe_rollback()
            raise
        return self._real.fetchone()[0]

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


# ---------------------------------------------------------------------------
# کانکشن
# ---------------------------------------------------------------------------
class PGConnection:
    def __init__(self, real_conn):
        self._real = real_conn

    def cursor(self):
        return PGCursor(self._real.cursor())

    def execute(self, sql, params=()):
        """معادلِ متدِ راحتِ sqlite3.Connection.execute (خودش یه کِرسرِ موقت می‌سازه).
        فقط برای PRAGMA صدا زده می‌شه تو کدِ فعلی، که همینجا بی‌اثره."""
        if _PRAGMA_ANY_RE.match(sql):
            return
        c = self.cursor()
        c.execute(sql, params)
        return c

    def commit(self):
        self._real.commit()

    def rollback(self):
        self._real.rollback()

    def close(self):
        pass  # کانکشن برای کلِ عمرِ پروسه باز می‌مونه (مثلِ نسخه‌ی SQLite)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def connect(database_url: str) -> PGConnection:
    if not _HAS_PSYCOPG2:
        raise RuntimeError(
            "برای اتصال به PostgreSQL باید psycopg2-binary نصب باشه. "
            "به requirements.txt اضافه‌اش کن: psycopg2-binary"
        )
    real = psycopg2.connect(database_url)
    real.autocommit = False
    return PGConnection(real)
