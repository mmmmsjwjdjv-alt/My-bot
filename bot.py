import os
import re
import time
import shutil
import sqlite3
import logging
import random
from urllib.parse import quote
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import TelegramError, BadRequest, Conflict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")

ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
CARD_NUMBER_DEFAULT = os.environ.get("CARD_NUMBER", "6219861351165898")
DB_WARNING = ""

def _resolve_db_path():
    """مسیر دیتابیس رو پیدا می‌کنه و مطمئن می‌شه واقعاً قابل نوشتنه (مشکل رایج Volume در Railway)."""
    global DB_WARNING
    candidates = []
    explicit = os.environ.get("DB_PATH")
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if explicit:
        candidates.append(explicit)
    if vol:
        candidates.append(os.path.join(vol, "bot.db"))
    candidates.append("/data/bot.db")
    errors = []
    for i, path in enumerate(candidates):
        try:
            d = os.path.dirname(path) or "."
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            sqlite3.connect(path).close()
            if errors:
                DB_WARNING = "؛ ".join(errors)
                logger.warning(f"مسیر اصلی دیتابیس کار نکرد، از {path} استفاده شد: {DB_WARNING}")
            return path
        except Exception as e:
            errors.append(f"{path}: {e}")
            logger.error(f"❌ مسیر دیتابیس {path} قابل استفاده نیست: {e}")
    # آخرین راه: کنار فایل ربات (با هر ری‌دیپلوی پاک می‌شه!)
    path = os.path.join(os.getcwd(), "bot.db")
    DB_WARNING = "؛ ".join(errors) + " — دیتابیس موقت ساخته شد و با هر ری‌دیپلوی پاک می‌شه!"
    logger.error("⚠️ " + DB_WARNING)
    return path

DB_PATH = _resolve_db_path()

if not BOT_TOKEN:
    raise RuntimeError("❌ متغیر محیطی BOT_TOKEN تنظیم نشده")
if not ADMIN_IDS:
    raise RuntimeError("❌ متغیر محیطی ADMIN_IDS تنظیم نشده")


BACK_BTN = "بازگشت 🔙"

PLAN_CATEGORIES = ["حجمی", "نامحدود"]
CATEGORY_LABELS = {
    "حجمی": "حجمی 📊",
    "نامحدود": "نامحدود ♾",
}

# ───────────────────────── کمکی‌های عمومی (پاسخ امن به callback، ادیت امن، روز ایران) ─────────────────────────
_ANSWERED = {}

async def qanswer(query, text=None, show_alert=False):
    """هر callback_query فقط یک‌بار می‌تونه جواب داده بشه؛ این تابع دفعه‌های بعدی رو بی‌صدا نادیده می‌گیره."""
    qid = getattr(query, "id", None)
    if qid is None or qid in _ANSWERED:
        return
    _ANSWERED[qid] = time.time()
    if len(_ANSWERED) > 3000:
        cutoff = time.time() - 600
        for k in [k for k, v in _ANSWERED.items() if v < cutoff]:
            _ANSWERED.pop(k, None)
    try:
        await query.answer(text=text, show_alert=show_alert)
    except TelegramError as e:
        logger.debug(f"answer_callback_query failed: {e}")

async def safe_edit(query, text, reply_markup=None, **kw):
    """edit_message_text که خطای «Message is not modified» رو نادیده می‌گیره."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, **kw)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        raise

def today_key():
    """تاریخ امروز به وقت ایران (برای کوین روزانه)."""
    return (datetime.utcnow() + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d")

def seconds_to_next_day():
    now = datetime.utcnow() + timedelta(hours=3, minutes=30)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((nxt - now).total_seconds())

def fmt_remaining(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h} ساعت و {m} دقیقه"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        wallet INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0,
        ban_reason TEXT,
        joined_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS admins(
        user_id INTEGER PRIMARY KEY
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, price INTEGER, duration_days INTEGER,
        is_test INTEGER DEFAULT 0,
        category TEXT DEFAULT 'حجمی'
    )""")
    try:
        c.execute("ALTER TABLE plans ADD COLUMN category TEXT DEFAULT 'حجمی'")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS plan_configs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER, content_type TEXT,
        text_content TEXT, photo_file_id TEXT, caption TEXT,
        used INTEGER DEFAULT 0, used_by INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS test_configs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_type TEXT, text_content TEXT, photo_file_id TEXT, caption TEXT,
        used INTEGER DEFAULT 0, used_by INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS test_usage(
        user_id INTEGER PRIMARY KEY, used_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, plan_id INTEGER, plan_title TEXT,
        price INTEGER, pay_method TEXT, status TEXT,
        config_id INTEGER, created_at TEXT, expires_at TEXT, is_renew INTEGER DEFAULT 0,
        hidden INTEGER DEFAULT 0
    )""")
    try:
        c.execute("ALTER TABLE orders ADD COLUMN hidden INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS wallet_charges(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, amount INTEGER, status TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, user_msg TEXT, status TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS discount_codes(
        code TEXT PRIMARY KEY, amount INTEGER, percent INTEGER,
        expires_at TEXT, max_uses INTEGER, used_count INTEGER DEFAULT 0, active INTEGER DEFAULT 1
    )""")
    try:
        c.execute("ALTER TABLE discount_codes ADD COLUMN percent INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE discount_codes ADD COLUMN target_scope TEXT DEFAULT 'all'")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE discount_codes ADD COLUMN target_value TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS discount_usage(
        code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS required_channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_username TEXT, channel_title TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS owners(
        user_id INTEGER PRIMARY KEY
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_settings(
        id INTEGER PRIMARY KEY CHECK (id=1),
        card_number TEXT,
        card_holder TEXT,
        maintenance_mode INTEGER DEFAULT 0,
        welcome_message TEXT
    )""")
    c.execute("INSERT OR IGNORE INTO bot_settings(id, card_number, card_holder, maintenance_mode) VALUES (1,?,?,0)",
              (CARD_NUMBER_DEFAULT, ""))
    try:
        c.execute("ALTER TABLE bot_settings ADD COLUMN welcome_message TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS wheel_settings(
        id INTEGER PRIMARY KEY CHECK (id=1),
        enabled INTEGER DEFAULT 0,
        cooldown_hours INTEGER DEFAULT 24
    )""")
    c.execute("INSERT OR IGNORE INTO wheel_settings(id, enabled, cooldown_hours) VALUES (1,0,24)")
    c.execute("""CREATE TABLE IF NOT EXISTS wheel_prizes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prize_type TEXT, weight INTEGER,
        discount_percent INTEGER, discount_hours INTEGER,
        config_gb REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS wheel_prize_configs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prize_id INTEGER, content_type TEXT,
        text_content TEXT, photo_file_id TEXT, caption TEXT,
        used INTEGER DEFAULT 0, used_by INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS wheel_spins(
        user_id INTEGER PRIMARY KEY, last_spin_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS admin_permissions(
        user_id INTEGER, permission TEXT,
        PRIMARY KEY (user_id, permission)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS button_colors(
        button_text TEXT PRIMARY KEY,
        color TEXT NOT NULL DEFAULT 'default'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS feature_settings(
        key TEXT PRIMARY KEY, value TEXT
    )""")
    for _k, _v in (("referral_enabled", "0"), ("daily_enabled", "0"), ("coinshop_enabled", "0"),
                   ("referral_reward", "5"), ("daily_reward", "3")):
        c.execute("INSERT OR IGNORE INTO feature_settings(key, value) VALUES (?,?)", (_k, _v))
    for _stmt in ("ALTER TABLE users ADD COLUMN coins INTEGER DEFAULT 0",
                  "ALTER TABLE plans ADD COLUMN is_coin INTEGER DEFAULT 0",
                  "ALTER TABLE plans ADD COLUMN coin_price INTEGER DEFAULT 0"):
        try:
            c.execute(_stmt)
        except sqlite3.OperationalError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS referrals(
        referred_id INTEGER PRIMARY KEY, referrer_id INTEGER, created_at TEXT, rewarded INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS coin_missions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, description TEXT, link TEXT,
        kind TEXT DEFAULT 'trust', channel TEXT, active INTEGER DEFAULT 1
    )""")
    try:
        c.execute("ALTER TABLE coin_missions ADD COLUMN wait_seconds INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS mission_seen(
        user_id INTEGER, mission_id INTEGER, day TEXT, seen_at REAL,
        PRIMARY KEY (user_id, mission_id, day)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS mission_done(
        user_id INTEGER, mission_id INTEGER, day TEXT,
        PRIMARY KEY (user_id, mission_id, day)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_claims(
        user_id INTEGER, day TEXT, PRIMARY KEY (user_id, day)
    )""")
    for aid in ADMIN_IDS:
        c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (aid,))
        c.execute("INSERT OR IGNORE INTO owners(user_id) VALUES (?)", (aid,))
    migrate_permissions(c)
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = get_conn()
    r = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return r is not None

def is_owner(user_id):
    conn = get_conn()
    r = conn.execute("SELECT 1 FROM owners WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return r is not None

def ensure_user(user):
    """کاربر رو ثبت/بروزرسانی می‌کنه؛ اگه کاربر جدید بود True برمی‌گردونه."""
    conn = get_conn()
    r = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,)).fetchone()
    is_new = r is None
    if is_new:
        conn.execute("INSERT INTO users(user_id, username, wallet, banned, joined_at) VALUES (?,?,0,0,?)",
                     (user.id, user.username or "", datetime.now().isoformat()))
    else:
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (user.username or "", user.id))
    conn.commit()
    conn.close()
    return is_new

def is_banned(user_id):
    conn = get_conn()
    r = conn.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return r is not None and r["banned"] == 1

def get_ban_reason(user_id):
    conn = get_conn()
    r = conn.execute("SELECT ban_reason FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return r["ban_reason"] if r and r["ban_reason"] else "دلیلی ثبت نشده است"

def get_wallet(user_id):
    conn = get_conn()
    r = conn.execute("SELECT wallet FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return r["wallet"] if r else 0

def change_wallet(user_id, delta):
    conn = get_conn()
    conn.execute("UPDATE users SET wallet = wallet + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def plan_capacity(plan_id):
    conn = get_conn()
    r = conn.execute("SELECT COUNT(*) as c FROM plan_configs WHERE plan_id=? AND used=0", (plan_id,)).fetchone()
    conn.close()
    return r["c"]

def get_all_admin_ids():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM admins").fetchall()
    conn.close()
    return [r['user_id'] for r in rows]

async def notify_all_admins_photo(context, photo_file_id, caption, reply_markup=None, perm=None):
    """عکس رو برای همه ادمین‌ها می‌فرسته؛ اگه یکیشون چت نداشته باشه بقیه رو متوقف نمی‌کنه."""
    for admin_id in get_admin_ids_with(perm):
        try:
            await context.bot.send_photo(admin_id, photo_file_id, caption=caption, reply_markup=reply_markup)
        except TelegramError as e:
            logger.warning(f"ارسال رسید به ادمین {admin_id} ناموفق بود: {e}")

async def notify_all_admins_text(context, text, reply_markup=None, perm=None):
    """پیام متنی رو برای همه ادمین‌ها می‌فرسته؛ اگه یکیشون چت نداشته باشه بقیه رو متوقف نمی‌کنه."""
    for admin_id in get_admin_ids_with(perm):
        try:
            await context.bot.send_message(admin_id, text, reply_markup=reply_markup)
        except TelegramError as e:
            logger.warning(f"ارسال پیام به ادمین {admin_id} ناموفق بود: {e}")

async def copy_message_to_admins(context, from_chat_id, message_id, reply_markup=None, perm=None):
    """
    پیام رو (هر نوعی که باشه: متن، عکس، فیلم، ویس، با بولد/اسپویلر/نقل‌قول و...)
    دقیقاً همون‌طوری که هست برای همه‌ی ادمین‌ها کپی می‌کنه — چون از copy_message تلگرام
    استفاده می‌شه، تمام فرمت‌بندی و نوع فایل بدون تغییر حفظ می‌شه.
    """
    ids = get_admin_ids_with(perm)
    for admin_id in ids:
        try:
            await context.bot.copy_message(
                chat_id=admin_id, from_chat_id=from_chat_id, message_id=message_id,
                reply_markup=reply_markup,
            )
        except TelegramError as e:
            logger.warning(f"کپی پیام به ادمین {admin_id} ناموفق بود: {e}")

async def copy_message_to_user(context, user_id, from_chat_id, message_id):
    """پاسخ پشتیبانی رو دقیقا با همون فرمت/نوع فایل (متن، عکس، فیلم، ویس و...) برای کاربر کپی می‌کنه."""
    try:
        await context.bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
        return True
    except TelegramError as e:
        logger.warning(f"ارسال پاسخ به کاربر {user_id} ناموفق بود: {e}")
        return False

def get_plan_duration(plan_id):
    conn = get_conn()
    r = conn.execute("SELECT duration_days FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    return r['duration_days'] if r else 30

def get_bot_settings():
    conn = get_conn()
    row = conn.execute("SELECT * FROM bot_settings WHERE id=1").fetchone()
    conn.close()
    return row

def get_card_info():
    s = get_bot_settings()
    return (s['card_number'], s['card_holder']) if s else (CARD_NUMBER_DEFAULT, "")

def set_card_info(number, holder):
    conn = get_conn()
    conn.execute("UPDATE bot_settings SET card_number=?, card_holder=? WHERE id=1", (number, holder))
    conn.commit()
    conn.close()

def get_custom_welcome_message():
    s = get_bot_settings()
    return s['welcome_message'] if s and s['welcome_message'] else None

def set_custom_welcome_message(text_or_none):
    conn = get_conn()
    conn.execute("UPDATE bot_settings SET welcome_message=? WHERE id=1", (text_or_none,))
    conn.commit()
    conn.close()

def build_welcome_message(bot_name):
    custom = get_custom_welcome_message()
    if custom:
        return custom.replace("{نام}", bot_name).replace("{bot_name}", bot_name)
    return f"به ربات فروش کانفیگ {bot_name} خوش آمدید 🌟\nیکی از گزینه‌ها رو انتخاب کنید:"

def is_maintenance_on():
    s = get_bot_settings()
    return bool(s['maintenance_mode']) if s else False

def set_maintenance(flag):
    conn = get_conn()
    conn.execute("UPDATE bot_settings SET maintenance_mode=? WHERE id=1", (int(flag),))
    conn.commit()
    conn.close()

# ───────────────────────── دسترسی‌های ادمین (تک‌تک دکمه‌های پنل) ─────────────────────────
PERMISSIONS = {
    "add_plan": "➕ افزودن پلن",
    "edit_plan": "✏️ ویرایش پلن",
    "del_plan": "🗑 حذف پلن",
    "add_test": "🎁 افزودن کانفیگ تست",
    "list_test": "📋 لیست کانفیگ تست",
    "edit_test": "✏️ ویرایش کانفیگ تست",
    "del_test": "🗑 حذف کانفیگ تست",
    "add_coin_plan": "🪙 افزودن کانفیگ کوین",
    "edit_coin_plan": "✏️ ویرایش کانفیگ کوین",
    "del_coin_plan": "🗑 حذف کانفیگ کوین",
    "list_coin_plan": "📋 لیست کانفیگ کوین",
    "coin_settings": "⚙️ تنظیمات کوین (زیرمجموعه، روزانه، ماموریت)",
    "users": "👥 مشاهده کاربران",
    "direct_msg": "✉️ پیام مستقیم به کاربر",
    "ban": "🚫 مسدود کردن کاربر",
    "unban": "✅ رفع مسدودی",
    "charge_wallet": "💳 شارژ کیف پول کاربران",
    "dec_wallet": "➖ کسر از کیف پول کاربران",
    "broadcast": "📢 پیام همگانی",
    "new_discount": "🏷 ساخت کد تخفیف",
    "revoke_discount": "❌ باطل کردن کد تخفیف",
    "add_channel": "📢 افزودن کانال اجباری",
    "del_channel": "🗑 حذف کانال اجباری",
    "change_card": "💳 تغییر شماره کارت",
    "welcome": "✏️ ویرایش پیام خوش‌آمد",
    "maintenance": "🔧 خاموش/روشن کردن ربات",
    "add_admin": "👤 افزودن ادمین جدید",
    "list_admins": "📋 لیست ادمین‌ها",
    "orders": "🧾 تایید/رد رسیدها و دریافت اعلان خرید",
    "support": "📩 پیام‌های پشتیبانی و پاسخ به تیکت‌ها",
}

# دسترسی‌های قدیمی (نسخه‌های قبل) → دسترسی‌های جدید؛ فقط یک‌بار موقع مهاجرت دیتابیس استفاده می‌شه
LEGACY_PERMISSION_MAP = {
    "plans": ["add_plan", "edit_plan", "del_plan"],
    "test": ["add_test", "list_test", "edit_test", "del_test"],
    "users": ["users", "direct_msg", "ban", "unban"],
    "wallet": ["charge_wallet", "dec_wallet"],
    "broadcast": ["broadcast"],
    "discount": ["new_discount", "revoke_discount"],
    "channels": ["add_channel", "del_channel"],
    "admins": ["add_admin"],
    "card": ["change_card"],
    "welcome": ["welcome"],
    "maintenance": ["maintenance"],
}
LEGACY_ONLY_KEYS = [k for k in LEGACY_PERMISSION_MAP if k not in PERMISSIONS]

def migrate_permissions(c):
    """تبدیل دسترسی‌های کلی قدیمی به دسترسی‌های جزئی جدید (فقط یک‌بار، علامتش داخل جدول meta ذخیره می‌شه)."""
    row = c.execute("SELECT value FROM meta WHERE key='perm_v2'").fetchone()
    if row:
        return
    legacy_rows = c.execute("SELECT user_id, permission FROM admin_permissions").fetchall()
    for r in legacy_rows:
        uid, perm = r[0], r[1]
        for newp in LEGACY_PERMISSION_MAP.get(perm, []):
            c.execute("INSERT OR IGNORE INTO admin_permissions(user_id, permission) VALUES (?,?)", (uid, newp))
    if LEGACY_ONLY_KEYS:
        q = ",".join("?" * len(LEGACY_ONLY_KEYS))
        c.execute(f"DELETE FROM admin_permissions WHERE permission IN ({q})", LEGACY_ONLY_KEYS)
    # قبلاً همه‌ی ادمین‌ها این سه مورد رو داشتن؛ برای حفظ رفتار قبلی به ادمین‌های فعلی داده می‌شه
    admins = c.execute("SELECT user_id FROM admins").fetchall()
    owners = {r[0] for r in c.execute("SELECT user_id FROM owners").fetchall()}
    for a in admins:
        if a[0] in owners:
            continue
        for p in ("orders", "support", "list_admins"):
            c.execute("INSERT OR IGNORE INTO admin_permissions(user_id, permission) VALUES (?,?)", (a[0], p))
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('perm_v2', '1')")

def get_admin_permissions(user_id):
    if is_owner(user_id):
        return set(PERMISSIONS.keys())
    conn = get_conn()
    rows = conn.execute("SELECT permission FROM admin_permissions WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    return {r['permission'] for r in rows if r['permission'] in PERMISSIONS}

def has_permission(user_id, perm):
    if is_owner(user_id):
        return True
    if not is_admin(user_id):
        return False
    return perm in get_admin_permissions(user_id)

def set_admin_permissions(user_id, perms):
    conn = get_conn()
    conn.execute("DELETE FROM admin_permissions WHERE user_id=?", (user_id,))
    for p in perms:
        if p in PERMISSIONS:
            conn.execute("INSERT OR IGNORE INTO admin_permissions(user_id, permission) VALUES (?,?)", (user_id, p))
    conn.commit()
    conn.close()

def get_admin_ids_with(perm=None):
    """آیدی ادمین‌هایی که یه دسترسی خاص رو دارن (مالک‌ها همیشه شامل می‌شن). perm=None یعنی همه‌ی ادمین‌ها."""
    conn = get_conn()
    admins = [r['user_id'] for r in conn.execute("SELECT user_id FROM admins").fetchall()]
    if perm is None:
        conn.close()
        return admins
    owners = {r['user_id'] for r in conn.execute("SELECT user_id FROM owners").fetchall()}
    with_perm = {r['user_id'] for r in conn.execute("SELECT user_id FROM admin_permissions WHERE permission=?", (perm,)).fetchall()}
    conn.close()
    return [a for a in admins if a in owners or a in with_perm]

# قوانین دسترسی برای دکمه‌های شیشه‌ای ادمین (جلوگیری از دستکاری callback_data)
CALLBACK_PERM_RULES = [
    (("editcat_", "editplan_sel_", "editfield_", "delcfg_", "viewcfg_", "setcat_", "cfgtype_", "bulkcfg_", "newplancat_"),
     ["add_plan", "edit_plan", "add_coin_plan", "edit_coin_plan"]),
    (("delcat_", "delplan_confirm_"), ["del_plan"]),
    (("testtype_", "edittest_sel_", "deltest_"), ["add_test", "edit_test", "del_test"]),
    (("revokecode_", "discscope"), ["new_discount", "revoke_discount"]),
    (("viewuserorders_", "adminvieworder_"), ["users"]),
    (("unban_",), ["unban"]),
    (("selnewadmin_",), ["add_admin"]),
    (("delchannel_",), ["del_channel"]),
    (("approve_order_", "reject_order_", "approve_charge_", "reject_charge_"), ["orders"]),
    (("reply_ticket_",), ["support"]),
    (("ecp_", "ecpf_"), ["edit_coin_plan"]),
    (("dcp_", "dcpok_"), ["del_coin_plan"]),
    (("cs_",), ["coin_settings"]),
    (("clr|", "resetbot_"), "OWNER"),
]

def _cb_denied(user_id, data):
    for prefixes, perms in CALLBACK_PERM_RULES:
        if data.startswith(prefixes):
            if is_owner(user_id):
                return None
            if perms == "OWNER":
                return "این قابلیت فقط برای مالک اصلی ربات است ❌"
            if not is_admin(user_id) or not any(has_permission(user_id, p) for p in perms):
                return "شما دسترسی لازم رو ندارید ❌"
            return None
    return None


WHEEL_PRIZE_LABELS = {
    "discount": "🏷 کد تخفیف",
    "config": "🎁 کانفیگ رایگان",
    "respin": "🔄 یک دور دیگر",
    "none": "پوچ ❌",
}

def get_wheel_settings():
    conn = get_conn()
    row = conn.execute("SELECT * FROM wheel_settings WHERE id=1").fetchone()
    conn.close()
    return row

def is_wheel_enabled():
    s = get_wheel_settings()
    return bool(s['enabled']) if s else False

def set_wheel_enabled(flag):
    conn = get_conn()
    conn.execute("UPDATE wheel_settings SET enabled=? WHERE id=1", (int(flag),))
    conn.commit()
    conn.close()

def set_wheel_cooldown(hours):
    conn = get_conn()
    conn.execute("UPDATE wheel_settings SET cooldown_hours=? WHERE id=1", (hours,))
    conn.commit()
    conn.close()

def get_wheel_prizes():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM wheel_prizes ORDER BY id").fetchall()
    conn.close()
    return rows

def add_wheel_prize(prize_type, weight, discount_percent=None, discount_hours=None, config_gb=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO wheel_prizes(prize_type, weight, discount_percent, discount_hours, config_gb) VALUES (?,?,?,?,?)",
        (prize_type, weight, discount_percent, discount_hours, config_gb)
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def delete_wheel_prize(prize_id):
    conn = get_conn()
    conn.execute("DELETE FROM wheel_prizes WHERE id=?", (prize_id,))
    conn.execute("DELETE FROM wheel_prize_configs WHERE prize_id=?", (prize_id,))
    conn.commit()
    conn.close()

def pick_weighted_prize(prizes):
    total = sum(p['weight'] for p in prizes) or 1
    r = random.randint(1, total)
    upto = 0
    for p in prizes:
        upto += p['weight']
        if r <= upto:
            return p
    return prizes[-1]

def check_wheel_cooldown(user_id):
    """اگه هنوز نوبتش نشده، دقیقه‌های باقی‌مونده رو برمی‌گردونه؛ وگرنه None."""
    settings = get_wheel_settings()
    conn = get_conn()
    row = conn.execute("SELECT * FROM wheel_spins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not row or not row['last_spin_at']:
        return None
    last = datetime.fromisoformat(row['last_spin_at'])
    passed = datetime.now() - last
    remain = timedelta(hours=settings['cooldown_hours']) - passed
    if remain.total_seconds() > 0:
        return remain
    return None

def update_wheel_spin_time(user_id):
    conn = get_conn()
    conn.execute("INSERT INTO wheel_spins(user_id, last_spin_at) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET last_spin_at=excluded.last_spin_at",
                 (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def generate_wheel_discount_code():
    return "WHL" + "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))

def mark_discount_used(code, user_id):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO discount_usage(code, user_id) VALUES (?,?)", (code, user_id))
    conn.execute("UPDATE discount_codes SET used_count = used_count + 1 WHERE code=?", (code,))
    conn.commit()
    conn.close()

def validate_discount(code, user_id, plan_id=None):
    conn = get_conn()
    row = conn.execute("SELECT * FROM discount_codes WHERE code=? AND active=1", (code,)).fetchone()
    if not row:
        conn.close()
        return None, "کد تخفیف نامعتبر است ❌"
    if row['expires_at'] and datetime.now() > datetime.fromisoformat(row['expires_at']):
        conn.close()
        return None, "کد تخفیف منقضی شده است ❌"
    if row['max_uses'] != -1 and row['used_count'] >= row['max_uses']:
        conn.close()
        return None, "ظرفیت استفاده از این کد تمام شده است ❌"
    used = conn.execute("SELECT 1 FROM discount_usage WHERE code=? AND user_id=?", (code, user_id)).fetchone()
    scope = row['target_scope'] if 'target_scope' in row.keys() else 'all'
    if scope and scope != 'all' and plan_id is not None:
        plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        conn.close()
        if scope == 'category':
            if not plan or plan['category'] != row['target_value']:
                return None, f"این کد تخفیف فقط برای پلن‌های «{CATEGORY_LABELS.get(row['target_value'], row['target_value'])}» معتبر است ❌"
        elif scope == 'plan':
            if not plan or str(plan['id']) != str(row['target_value']):
                return None, "این کد تخفیف فقط برای یک پلن خاص معتبر است ❌"
    else:
        conn.close()
    if used:
        return None, "شما قبلا از این کد استفاده کرده‌اید ❌"
    return row, None

def clear_state(context):
    context.user_data['state'] = None
    context.user_data['data'] = {}

async def reply_or_edit(update, text, reply_markup=None):
    """اگه از دکمه شیشه‌ای اومده باشه ادیت می‌کنه، اگه از متن اومده باشه پیام جدید می‌فرسته."""
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

def get_required_channels():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM required_channels").fetchall()
    conn.close()
    return rows

async def get_missing_channels(context, user_id):
    channels = get_required_channels()
    missing = []
    for ch in channels:
        try:
            member = await context.bot.get_chat_member(f"@{ch['channel_username']}", user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except TelegramError:

            missing.append(ch)
    return missing

async def send_join_prompt(update, context, missing):
    kb = []
    for ch in missing:
        kb.append([IKB(f"عضویت در {ch['channel_title']} 📢", url=f"https://t.me/{ch['channel_username']}")])
    kb.append([IKB("عضو شدم، بررسی کن ✅", callback_data="check_join")])
    text = "برای استفاده از ربات، ابتدا باید در کانال‌های زیر عضو شوید:"
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        except TelegramError:
            await context.bot.send_message(update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    missing = await get_missing_channels(context, user_id)
    if missing:

        await qanswer(query, "❌ کاربر گرامی، شما هنوز عضو تمام کانال‌های اجباری نشده‌اید. لطفا عضو شوید و دوباره تلاش کنید.", show_alert=True)
        return
    await query.edit_message_text("✅ عضویت شما تایید شد. حالا می‌توانید از ربات استفاده کنید.")
    await context.bot.send_message(update.effective_chat.id, "یکی از گزینه‌ها رو انتخاب کنید:", reply_markup=main_menu_kb(user_id))
    await reward_referral_if_pending(context, user_id)

# ───────────────────────── رنگ دکمه‌ها (کیبورد معمولی + دکمه‌های شیشه‌ای) ─────────────────────────
STYLE_VALUES = ("primary", "success", "danger")
COLOR_NAMES = {
    "default": "⚪ پیش‌فرض",
    "danger": "🔴 قرمز",
    "success": "🟢 سبز",
    "primary": "🔵 آبی",
}
COLOR_ICONS = {"default": "⚪", "danger": "🔴", "success": "🟢", "primary": "🔵"}

def _detect_style_support():
    """چک می‌کنه نسخه‌ی python-telegram-bot پارامتر style (رنگ دکمه) رو داره یا نه."""
    try:
        KeyboardButton("x", style="primary")
        InlineKeyboardButton("x", callback_data="x", style="primary")
        return True
    except Exception:
        return False

COLORS_SUPPORTED = _detect_style_support()
if not COLORS_SUPPORTED:
    logger.warning("⚠️ نسخه‌ی python-telegram-bot از رنگ دکمه‌ها پشتیبانی نمی‌کنه. "
                   "requirements.txt رو روی python-telegram-bot[job-queue]>=22.7 بذار.")

_COLOR_CACHE = None

def invalidate_color_cache():
    global _COLOR_CACHE
    _COLOR_CACHE = None

def _load_colors():
    global _COLOR_CACHE
    if _COLOR_CACHE is None:
        try:
            conn = get_conn()
            rows = conn.execute("SELECT button_text, color FROM button_colors").fetchall()
            conn.close()
            _COLOR_CACHE = {r[0]: r[1] for r in rows}
        except Exception:
            return {}
    return _COLOR_CACHE

def get_button_color(button_key):
    return _load_colors().get(button_key, "default")

def set_button_color(button_key, color):
    if color not in ("default",) + STYLE_VALUES:
        color = "default"
    conn = get_conn()
    conn.execute("INSERT INTO button_colors(button_text,color) VALUES(?,?) "
                 "ON CONFLICT(button_text) DO UPDATE SET color=excluded.color", (button_key, color))
    conn.commit()
    conn.close()
    invalidate_color_cache()

def reset_all_button_colors():
    conn = get_conn()
    conn.execute("DELETE FROM button_colors")
    conn.commit()
    conn.close()
    invalidate_color_cache()

def styled_reply_keyboard(rows):
    """کیبورد معمولی (پایین صفحه) با رنگ ذخیره‌شده‌ی هر دکمه."""
    styled_rows = []
    for row in rows:
        styled_row = []
        for text in row:
            color = get_button_color(text) if COLORS_SUPPORTED else "default"
            if color in STYLE_VALUES:
                styled_row.append(KeyboardButton(text=text, style=color))
            else:
                styled_row.append(KeyboardButton(text=text))
        styled_rows.append(styled_row)
    return ReplyKeyboardMarkup(styled_rows, resize_keyboard=True)

_CAT_HEADS = ("buycat", "editcat", "delcat", "newplancat", "setcat")
_PLAN_PREFIXES = ("editplan_sel_", "delplan_confirm_", "discscopeplan_", "coinplan_", "buyplan_")

def _auto_key(text, callback_data, url):
    """کلید پایدار رنگ برای هر دکمه‌ی شیشه‌ای؛ '' یعنی این دکمه رنگی نمی‌شه."""
    if url:
        if url.startswith("tg://user"):
            return "i:viewuser"
        if "t.me/share" in url:
            return "i:sharelink"
        return "i:joinchannel"
    cd = callback_data or ""
    if not cd:
        return ""
    if cd == "cancel_action":
        return "i:cancel:" + re.sub(r"\d+", "", text or "").strip()
    if cd.startswith(("permtoggle_", "clr|", "noop")):
        return ""
    if cd.startswith("revokecode_"):
        return "i:revokecode"
    if cd.startswith(_PLAN_PREFIXES):
        return "plan:" + cd.rsplit("_", 1)[-1]
    parts = [p for p in cd.split("_") if p and not p.lstrip("-").isdigit()]
    if not parts:
        return "i:" + cd
    if parts[0] in _CAT_HEADS and len(parts) > 1:
        return "category:" + parts[-1]
    return "i:" + "_".join(parts)

def IKB(text, callback_data=None, url=None, ckey=None, **kw):
    """ساخت InlineKeyboardButton (دکمه‌ی شیشه‌ای) با رنگ ذخیره‌شده؛ جایگزین مستقیم InlineKeyboardButton."""
    key = ckey if ckey is not None else _auto_key(text, callback_data, url)
    args = {}
    if callback_data is not None:
        args["callback_data"] = callback_data
    if url is not None:
        args["url"] = url
    style = kw.pop("style", None)
    args.update(kw)
    if COLORS_SUPPORTED:
        if style is None and key:
            c = get_button_color(key)
            if c in STYLE_VALUES:
                style = c
        if style in STYLE_VALUES:
            args["style"] = style
    return InlineKeyboardButton(text, **args)

def styled_inline_button(text, callback_data, color_key):
    return IKB(text, callback_data=callback_data, ckey=color_key)


COIN_MENU_TXT = "کوین‌های من 🪙"
MAINT_ON_TXT = "روشن کردن ربات 🟢"
MAINT_OFF_TXT = "خاموش کردن ربات 🔴"
WHEEL_ON_TXT = "روشن کردن گردونه 🎡"
WHEEL_OFF_TXT = "خاموش کردن گردونه 🎡"
COLOR_MENU_TXT = "🎨 تغییر رنگ دکمه‌ها"
RESET_MENU_TXT = "🧨 ریست کل ربات"

# هر ردیف از پنل مدیریت: (متن دکمه، کلید دسترسی). دکمه‌ای که کاربر دسترسی‌ش رو نداشته باشه نمایش داده نمی‌شه.
ADMIN_LAYOUT = [
    [("افزودن پلن ➕", "add_plan"), ("ویرایش پلن ✏️", "edit_plan")],
    [("حذف پلن 🗑", "del_plan")],
    [("افزودن کانفیگ تست 🎁", "add_test"), ("لیست کانفیگ تست 📋", "list_test")],
    [("ویرایش کانفیگ تست ✏️", "edit_test"), ("حذف کانفیگ تست 🗑", "del_test")],
    [("افزودن کانفیگ کوین 🪙", "add_coin_plan"), ("ویرایش کانفیگ کوین ✏️", "edit_coin_plan")],
    [("حذف کانفیگ کوین 🗑", "del_coin_plan"), ("لیست کانفیگ کوین 📋", "list_coin_plan")],
    [("تنظیمات کوین ⚙️", "coin_settings")],
    [("کاربران 👥", "users"), ("پیام مستقیم ✉️", "direct_msg")],
    [("مسدود کردن 🚫", "ban"), ("رفع مسدودی ✅", "unban")],
    [("شارژ کیف پول 💳", "charge_wallet"), ("کسر از کیف پول ➖", "dec_wallet")],
    [("پیام همگانی 📢", "broadcast")],
    [("کد تخفیف جدید 🏷", "new_discount"), ("باطل کردن کد تخفیف ❌", "revoke_discount")],
    [("کانال اجباری ➕", "add_channel"), ("حذف کانال اجباری 🗑", "del_channel")],
    [("تغییر شماره کارت 💳", "change_card")],
    [("✏️ ویرایش پیام خوش‌آمد", "welcome")],
    [("@MAINT", "maintenance")],
    [("افزودن ادمین 👤", "add_admin"), ("لیست ادمین‌ها 📋", "list_admins")],
]

# دکمه‌های مخصوص مالک: (متن، شناسه)
OWNER_LAYOUT = [
    [("حذف ادمین 🗑", "owner_del_admin"), ("ویرایش دسترسی ادمین‌ها 🔑", "owner_edit_perms")],
    [("انتقال مالکیت ♻️", "owner_transfer")],
    [("@WHEEL", "owner_wheel_toggle"), ("مدیریت جایزه‌های گردونه ⚙️", "owner_wheel_manage")],
    [("دریافت فایل بکاپ 💾", "owner_backup"), ("بازگردانی از بکاپ 📥", "owner_restore")],
    [(COLOR_MENU_TXT, "owner_colors"), (RESET_MENU_TXT, "owner_reset")],
]
OWNER_SLUGS = {slug for row in OWNER_LAYOUT for (_t, slug) in row}

def _build_text_maps():
    text_to_slug = {}
    for row in ADMIN_LAYOUT + OWNER_LAYOUT:
        for t, slug in row:
            if t == "@MAINT":
                text_to_slug[MAINT_ON_TXT] = slug
                text_to_slug[MAINT_OFF_TXT] = slug
            elif t == "@WHEEL":
                text_to_slug[WHEEL_ON_TXT] = slug
                text_to_slug[WHEEL_OFF_TXT] = slug
            else:
                text_to_slug[t] = slug
    return text_to_slug

TEXT_TO_SLUG = _build_text_maps()
ADMIN_MENU_TEXTS = set(TEXT_TO_SLUG.keys())

MAIN_MENU_ORDER = [
    "خرید کانفیگ 🛒", "دریافت تست 🎁", "کیف پول 💰", "کانفیگ های من 📦",
    "پیام به پشتیبانی 📞", COIN_MENU_TXT, "گردونه شانس 🎡", "مدیریت 🛠",
]
MAIN_MENU_TEXTS = set(MAIN_MENU_ORDER)
ALL_MENU_TEXTS = MAIN_MENU_TEXTS | ADMIN_MENU_TEXTS | {BACK_BTN}

def main_menu_kb(user_id):
    rows = [
        ["خرید کانفیگ 🛒", "دریافت تست 🎁"],
        ["کیف پول 💰", "کانفیگ های من 📦"],
        ["پیام به پشتیبانی 📞"],
    ]
    if coin_features_on():
        rows.append([COIN_MENU_TXT])
    if is_wheel_enabled() and get_wheel_prizes():
        rows.append(["گردونه شانس 🎡"])
    if is_admin(user_id):
        rows.append(["مدیریت 🛠"])
    return styled_reply_keyboard(rows)

def admin_menu_kb(user_id):
    owner = is_owner(user_id)
    perms = get_admin_permissions(user_id)
    rows = []
    for layout_row in ADMIN_LAYOUT:
        row = []
        for t, slug in layout_row:
            if not (owner or slug in perms):
                continue
            if t == "@MAINT":
                t = MAINT_ON_TXT if is_maintenance_on() else MAINT_OFF_TXT
            row.append(t)
        if row:
            rows.append(row)
    if owner:
        for layout_row in OWNER_LAYOUT:
            row = []
            for t, _slug in layout_row:
                if t == "@WHEEL":
                    t = WHEEL_OFF_TXT if is_wheel_enabled() else WHEEL_ON_TXT
                row.append(t)
            rows.append(row)
    rows.append([BACK_BTN])
    return styled_reply_keyboard(rows)

def cancel_kb():
    return InlineKeyboardMarkup([[IKB("لغو ❌", callback_data="cancel_action")]])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = ensure_user(user)
    if is_new and context.args and feature_on("referral"):
        arg = context.args[0]
        if arg.startswith("ref_") and arg[4:].isdigit():
            register_referral(user.id, int(arg[4:]))
    if is_banned(user.id):
        await update.message.reply_text(f"کاربر محترم شما از طرف ادمین مسدود شده اید ❌\nدلیل: {get_ban_reason(user.id)}")
        return
    if is_maintenance_on() and not is_admin(user.id):
        await update.message.reply_text("🔧 ربات موقتاً برای تعمیرات خاموش است. لطفا کمی بعد دوباره تلاش کنید.")
        return
    clear_state(context)
    if not is_admin(user.id):
        missing = await get_missing_channels(context, user.id)
        if missing:
            await send_join_prompt(update, context, missing)
            return
    await update.message.reply_text(
        build_welcome_message(context.bot.first_name),
        reply_markup=main_menu_kb(user.id)
    )
    await reward_referral_if_pending(context, user.id)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    if is_banned(user.id):
        await update.message.reply_text(f"کاربر محترم شما از طرف ادمین مسدود شده اید ❌\nدلیل: {get_ban_reason(user.id)}")
        return
    if is_maintenance_on() and not is_admin(user.id):
        await update.message.reply_text("🔧 ربات موقتاً برای تعمیرات خاموش است. لطفا کمی بعد دوباره تلاش کنید.")
        return

    text = update.message.text.strip() if update.message.text else ""
    state = context.user_data.get('state')

    if text == BACK_BTN:
        clear_state(context)
        await update.message.reply_text("بازگشت به منوی اصلی:", reply_markup=main_menu_kb(user.id))
        return

    if state and text in ALL_MENU_TEXTS:
        clear_state(context)
        state = None

    if state:
        handled = await handle_stateful_text(update, context, state, text)
        if handled:
            return

    if not is_admin(user.id) and text not in ("مدیریت 🛠",):
        missing = await get_missing_channels(context, user.id)
        if missing:
            await send_join_prompt(update, context, missing)
            return

    if is_admin(user.id) and text in ADMIN_MENU_TEXTS:
        await admin_menu_dispatch(update, context, text)
        return

    if text == "خرید کانفیگ 🛒":
        await ask_buy_category(update, context)
    elif text == "دریافت تست 🎁":
        await handle_get_test(update, context)
    elif text == "کیف پول 💰":
        await show_wallet(update, context)
    elif text == "کانفیگ های من 📦":
        await show_my_configs(update, context)
    elif text == "گردونه شانس 🎡":
        await show_wheel_entry(update, context)
    elif text == COIN_MENU_TXT:
        await show_coin_home(update, context)
    elif text == "پیام به پشتیبانی 📞":
        context.user_data['state'] = "waiting_support_msg"
        await update.message.reply_text("لطفا پیام خودتون رو برای پشتیبانی بنویسید و ارسال کنید:")
    elif text == "مدیریت 🛠":
        if not is_admin(user.id):
            await update.message.reply_text("شما دسترسی ادمین ندارید ❌")
        elif is_owner(user.id) and not get_card_info()[1]:
            await update.message.reply_text(
                "👋 خوش اومدی! قبل از هر چیز، برای شروع باید شماره کارت دریافت وجه رو تنظیم کنی.\n\n"
                "شماره کارت رو ارسال کن:"
            )
            context.user_data['state'] = "admin_card_number"
            context.user_data['data'] = {}
        else:
            await update.message.reply_text("🛠 پنل مدیریت — یکی از گزینه‌ها رو انتخاب کنید:", reply_markup=admin_menu_kb(user.id))
    else:
        if not state:
            await update.message.reply_text("لطفا از دکمه‌های منو استفاده کنید 👇", reply_markup=main_menu_kb(user.id))

async def ask_buy_category(update, context):
    kb = [[styled_inline_button(CATEGORY_LABELS[c], f"buycat_{c}", f"category:{c}")] for c in PLAN_CATEGORIES]
    kb.append([IKB("انصراف ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدوم دسته کانفیگ رو می‌خوای؟ 👇", reply_markup=InlineKeyboardMarkup(kb))

async def show_plans_in_category(update, context, category, mode="buy"):
    conn = get_conn()
    plans = conn.execute("SELECT * FROM plans WHERE is_test=0 AND COALESCE(is_coin,0)=0 AND category=?", (category,)).fetchall()
    conn.close()
    label = CATEGORY_LABELS.get(category, category)
    if not plans:
        text = f"در حال حاضر پلنی در دسته «{label}» ثبت نشده است."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return
    kb = []
    prefix = "buyplan" if mode == "buy" else "renewplan"
    for p in plans:
        cap = plan_capacity(p['id'])
        plan_label = f"{p['title']} | {p['price']:,} تومان | موجودی: {cap}"
        kb.append([styled_inline_button(plan_label, f"{prefix}_{p['id']}", f"plan:{p['id']}")])
    kb.append([IKB("بازگشت 🔙", callback_data="cancel_action")])
    text = f"{label}\nیکی از پلن‌ها رو انتخاب کنید:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_select_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id, is_renew=False):
    query = update.callback_query
    conn = get_conn()
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    if not plan:
        await query.edit_message_text("این پلن دیگر موجود نیست ❌")
        return
    if plan_capacity(plan_id) <= 0:
        await query.edit_message_text("ظرفیت این پلن تمام شده است ❌")
        return

    context.user_data['data'] = {
        "plan_id": plan_id, "plan_title": plan['title'],
        "price": plan['price'], "is_renew": is_renew,
        "discount_code": None, "discount_amount": 0
    }
    context.user_data['state'] = "ask_discount"
    kb = InlineKeyboardMarkup([
        [IKB("دارم ✅", callback_data="discount_yes"),
         IKB("ندارم ❌", callback_data="discount_no")]
    ])
    await query.edit_message_text(f"پلن انتخابی: {plan['title']}\nقیمت: {plan['price']:,} تومان\n\nآیا کد تخفیف دارید؟", reply_markup=kb)

async def cb_discount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, has_code: bool):
    query = update.callback_query
    if not has_code:
        await ask_payment_method(update, context)
        return
    context.user_data['state'] = "waiting_discount_code"
    await query.edit_message_text("لطفا کد تخفیف رو ارسال کنید:")

async def ask_payment_method(update, context):
    d = context.user_data['data']
    price = d['price'] - d.get('discount_amount', 0)
    if price < 0:
        price = 0
    d['final_price'] = price
    context.user_data['state'] = "waiting_payment_method"
    kb = InlineKeyboardMarkup([
        [IKB("کیف پول 💰", callback_data="pay_wallet"),
         IKB("کارت به کارت 💳", callback_data="pay_card")]
    ])
    txt = f"مبلغ قابل پرداخت: {price:,} تومان\nروش خرید رو انتخاب کنید:"
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=kb)
    else:
        await update.message.reply_text(txt, reply_markup=kb)

async def cb_pay_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    d = context.user_data.get('data', {})
    price = d.get('final_price', d.get('price', 0))
    balance = get_wallet(user_id)
    if balance < price:
        await query.edit_message_text(f"موجودی کیف پول کافی نیست ❌\nموجودی شما: {balance:,} تومان")
        clear_state(context)
        return
    ok = await deliver_config_and_finalize(context, user_id, d, pay_method="کیف پول", auto_approved=True)
    if ok:
        change_wallet(user_id, -price)
        await query.edit_message_text("✅ خرید با موفقیت انجام شد و کانفیگ ارسال گردید.")
        if d.get('discount_code'):
            discount_line = f"🏷 کد تخفیف: {d['discount_code']} (تخفیف: {d.get('discount_amount', 0):,} تومان)"
        else:
            discount_line = "🏷 بدون کد تخفیف"
        await notify_all_admins_text(
            context,
            f"💰 خرید جدید با کیف پول\nکاربر: {user_id} (@{user.username or '-'})\n"
            f"پلن: {d['plan_title']}\n{discount_line}\nمبلغ پرداختی: {price:,} تومان\n"
            f"موجودی باقی‌مانده: {get_wallet(user_id):,} تومان"
        )
    else:
        await query.edit_message_text("ظرفیت این پلن تمام شده است ❌")
    clear_state(context)

async def cb_pay_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['state'] = "waiting_receipt_photo"
    card_number, card_holder = get_card_info()
    holder_line = f"\n👤 به نام: {card_holder}" if card_holder else ""
    await query.edit_message_text(
        f"لطفا مبلغ ({context.user_data['data'].get('final_price', 0):,} تومان) رو به شماره کارت زیر واریز کنید و رسید رو ارسال کنید:\n\n"
        f"💳 {card_number}{holder_line}\n\nلطفا رسید (عکس) رو برای ربات ارسال کنید 🧾♥️"
    )

async def deliver_config_and_finalize(context, user_id, data, pay_method, auto_approved=False):
    plan_id = data['plan_id']
    conn = get_conn()
    cfg = conn.execute("SELECT * FROM plan_configs WHERE plan_id=? AND used=0 LIMIT 1", (plan_id,)).fetchone()
    if not cfg:
        conn.close()
        return False
    conn.execute("UPDATE plan_configs SET used=1, used_by=? WHERE id=?", (user_id, cfg['id']))
    expires = (datetime.now() + timedelta(days=get_plan_duration(plan_id))).isoformat()
    conn.execute("""INSERT INTO orders(user_id, plan_id, plan_title, price, pay_method, status, config_id, created_at, expires_at, is_renew)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (user_id, plan_id, data['plan_title'], data.get('final_price', data['price']),
                  pay_method, "تحویل شد", cfg['id'], datetime.now().isoformat(), expires, int(data.get('is_renew', False))))
    conn.commit()
    conn.close()
    if data.get('discount_code'):
        mark_discount_used(data['discount_code'], user_id)
    await send_config_to_user(context, user_id, cfg)
    return True

async def send_config_to_user(context, user_id, cfg_row):
    if cfg_row['content_type'] == 'photo':
        await context.bot.send_photo(user_id, cfg_row['photo_file_id'], caption=cfg_row['caption'] or "")
    else:
        await context.bot.send_message(user_id, cfg_row['text_content'])

async def handle_get_test(update, context):
    user_id = update.effective_user.id
    conn = get_conn()
    used = conn.execute("SELECT 1 FROM test_usage WHERE user_id=?", (user_id,)).fetchone()
    if used:
        conn.close()
        await update.message.reply_text("شما قبلا از تست استفاده کرده اید ❌🫵")
        return
    rows = conn.execute("SELECT * FROM test_configs WHERE used=0").fetchall()
    if not rows:
        conn.close()
        await update.message.reply_text("در حال حاضر کانفیگ تستی موجود نیست.")
        return
    chosen = random.choice(rows)
    conn.execute("UPDATE test_configs SET used=1, used_by=? WHERE id=?", (user_id, chosen['id']))
    conn.execute("INSERT INTO test_usage(user_id, used_at) VALUES (?,?)", (user_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    await send_config_to_user(context, user_id, chosen)
    await update.message.reply_text("✅ کانفیگ تست برای شما ارسال شد.")

async def show_wallet(update, context):
    user_id = update.effective_user.id
    balance = get_wallet(user_id)
    kb = InlineKeyboardMarkup([[IKB("شارژ کیف پول 💳", callback_data="charge_wallet")]])
    await update.message.reply_text(f"💰 موجودی کیف پول شما: {balance:,} تومان", reply_markup=kb)

async def cb_charge_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['state'] = "waiting_charge_amount"
    await query.edit_message_text("مبلغ مورد نظر برای شارژ کیف پول رو به تومان وارد کنید (مثلا 100000):")

async def show_my_configs(update, context):
    user_id = update.effective_user.id
    conn = get_conn()
    orders = conn.execute("SELECT * FROM orders WHERE user_id=? AND hidden=0 ORDER BY id DESC", (user_id,)).fetchall()
    conn.close()
    if not orders:
        await update.message.reply_text("شما تاکنون هیچ کانفیگی خریداری نکرده‌اید.")
        return
    for o in orders:
        expires = datetime.fromisoformat(o['expires_at']) if o['expires_at'] else None
        if expires:
            remaining = expires - datetime.now()
            status = "✅ فعال" if remaining.total_seconds() > 0 else "❌ منقضی شده"
            remain_txt = f"{remaining.days} روز مانده" if remaining.total_seconds() > 0 else "تمام شده"
        else:
            status, remain_txt = "در انتظار تایید", "-"
        if o['pay_method'] == "کوین":
            kb_rows = [[IKB("تمدید با کوین 🔄", callback_data=f"coinplan_{o['plan_id']}")]]
        else:
            kb_rows = [[IKB("تمدید این پلن 🔄", callback_data=f"renewplan_{o['plan_id']}")]]
        row2 = []
        if o['config_id']:
            row2.append(IKB("مشاهده کانفیگ 👁", callback_data=f"vieworder_{o['id']}"))
        row2.append(IKB("حذف از لیست 🗑", callback_data=f"hideorder_{o['id']}"))
        kb_rows.append(row2)
        await update.message.reply_text(
            f"📦 پلن: {o['plan_title']}\n💵 قیمت: {_order_price_text(o)}\nوضعیت: {status}\n⏳ {remain_txt}",
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )

async def cb_view_order(update, context, order_id):
    query = update.callback_query
    user_id = update.effective_user.id
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
    cfg = conn.execute("SELECT * FROM plan_configs WHERE id=?", (order['config_id'],)).fetchone() if order and order['config_id'] else None
    conn.close()
    if not order or not cfg:
        await qanswer(query, "کانفیگ این خرید دیگر در دسترس نیست.", show_alert=True)
        return
    await qanswer(query, )
    if cfg['content_type'] == 'photo':
        await context.bot.send_photo(user_id, cfg['photo_file_id'], caption=cfg['caption'] or "")
    else:
        await context.bot.send_message(user_id, cfg['text_content'])

async def cb_hide_order(update, context, order_id):
    query = update.callback_query
    user_id = update.effective_user.id
    conn = get_conn()
    conn.execute("UPDATE orders SET hidden=1 WHERE id=? AND user_id=?", (order_id, user_id))
    conn.commit()
    conn.close()
    await qanswer(query, "از لیست حذف شد ✅", show_alert=True)
    try:
        await query.message.delete()
    except Exception:
        pass

async def show_wheel_entry(update, context):
    if not is_wheel_enabled() or not get_wheel_prizes():
        await update.message.reply_text("گردونه شانس در حال حاضر فعال نیست.")
        return
    remain = check_wheel_cooldown(update.effective_user.id)
    if remain:
        hours = int(remain.total_seconds() // 3600)
        minutes = int((remain.total_seconds() % 3600) // 60)
        await update.message.reply_text(f"⏳ برای چرخوندن دوباره گردونه باید صبر کنی.\nزمان باقی‌مانده: {hours} ساعت و {minutes} دقیقه")
        return
    kb = InlineKeyboardMarkup([[IKB("بچرخون 🎡", callback_data="wheel_spin")]])
    await update.message.reply_text("🎡 آماده‌ای شانستو امتحان کنی؟", reply_markup=kb)

async def cb_wheel_spin(update, context):
    query = update.callback_query
    user_id = update.effective_user.id
    if not is_wheel_enabled():
        await query.edit_message_text("گردونه شانس در حال حاضر فعال نیست.")
        return
    remain = check_wheel_cooldown(user_id)
    if remain:
        hours = int(remain.total_seconds() // 3600)
        minutes = int((remain.total_seconds() % 3600) // 60)
        await qanswer(query, f"⏳ باید صبر کنی. زمان باقی‌مانده: {hours} ساعت و {minutes} دقیقه", show_alert=True)
        return
    prizes = get_wheel_prizes()
    if not prizes:
        await query.edit_message_text("گردونه شانس در حال حاضر فعال نیست.")
        return
    prize = pick_weighted_prize(prizes)
    ptype = prize['prize_type']

    if ptype == "respin":
        await query.edit_message_text("🔄 شانس آوردی! یه دور دیگه بچرخون 🎡", reply_markup=InlineKeyboardMarkup([[IKB("بچرخون 🎡", callback_data="wheel_spin")]]))
        return

    update_wheel_spin_time(user_id)

    if ptype == "none":
        await query.edit_message_text("😢 پوچ! دفعه بعد شانستو امتحان کن.")
        return

    if ptype == "discount":
        code = generate_wheel_discount_code()
        expires = (datetime.now() + timedelta(hours=prize['discount_hours'])).isoformat()
        conn = get_conn()
        conn.execute("INSERT OR REPLACE INTO discount_codes(code, amount, percent, expires_at, max_uses, used_count, active) VALUES (?,?,?,?,?,0,1)",
                     (code, 0, prize['discount_percent'], expires, 1))
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"🎉 تبریک! برنده‌ی {prize['discount_percent']}% تخفیف شدی.\n"
            f"کد تخفیفت (بزن روش کپی بشه):\n`{code}`\n\n⏳ اعتبار: {prize['discount_hours']} ساعت",
            parse_mode="Markdown"
        )
        user = update.effective_user
        await notify_all_admins_text(
            context,
            f"🎡 برد گردونه شانس\nکاربر: {user_id} (@{user.username or '-'})\n"
            f"جایزه: {prize['discount_percent']}% تخفیف — کد: {code} (اعتبار {prize['discount_hours']} ساعت)"
        )
        return

    if ptype == "config":
        conn = get_conn()
        cfg = conn.execute("SELECT * FROM wheel_prize_configs WHERE prize_id=? AND used=0 LIMIT 1", (prize['id'],)).fetchone()
        if cfg:
            conn.execute("UPDATE wheel_prize_configs SET used=1, used_by=? WHERE id=?", (user_id, cfg['id']))
            conn.commit()
        conn.close()
        if not cfg:
            await query.edit_message_text("🎉 برنده‌ی یک کانفیگ رایگان شدی، ولی موجودیش تموم شده! لطفا با پشتیبانی تماس بگیر.")
            return
        await query.edit_message_text(f"🎉 تبریک! برنده‌ی {prize['config_gb']:g} گیگ کانفیگ رایگان شدی 🎁")
        if cfg['content_type'] == 'photo':
            await context.bot.send_photo(user_id, cfg['photo_file_id'], caption=cfg['caption'] or "")
        else:
            await context.bot.send_message(user_id, cfg['text_content'])
        user = update.effective_user
        await notify_all_admins_text(
            context,
            f"🎡 برد گردونه شانس\nکاربر: {user_id} (@{user.username or '-'})\n"
            f"جایزه: کانفیگ رایگان {prize['config_gb']:g} گیگ"
        )
        return

async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        return
    state = context.user_data.get('state')
    photo_file_id = update.message.photo[-1].file_id

    if state == "waiting_support_msg":
        await submit_support_ticket(update, context)
        return

    if state == "admin_reply_ticket_text" and is_admin(user.id):
        await admin_finish_ticket_reply(update, context)
        return

    if state == "waiting_receipt_photo":
        d = context.user_data['data']
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO orders(user_id, plan_id, plan_title, price, pay_method, status, config_id, created_at, expires_at, is_renew) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user.id, d['plan_id'], d['plan_title'], d.get('final_price', d['price']), "کارت به کارت", "در انتظار تایید", None,
             datetime.now().isoformat(), "", int(d.get('is_renew', False)))
        )
        order_id = cur.lastrowid
        conn.commit()
        conn.close()

        kb = InlineKeyboardMarkup([
            [IKB("تایید رسید ✅", callback_data=f"approve_order_{order_id}"),
             IKB("رد رسید ❌", callback_data=f"reject_order_{order_id}")]
        ])
        caption = (f"🧾 رسید جدید\nکاربر: {user.id} (@{user.username or '-'})\n"
                   f"پلن: {d['plan_title']}\nمبلغ: {d.get('final_price', d['price']):,} تومان")
        await notify_all_admins_photo(context, photo_file_id, caption, kb, perm="orders")

        await update.message.reply_text("رسید شما دریافت شد و برای بررسی به ادمین ارسال گردید. لطفا منتظر بمانید ⏳")
        clear_state(context)
        return

    if state == "waiting_wallet_receipt_photo":
        d = context.user_data['data']
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO wallet_charges(user_id, amount, status, created_at) VALUES (?,?,?,?)",
            (user.id, d['charge_amount'], "در انتظار تایید", datetime.now().isoformat())
        )
        charge_id = cur.lastrowid
        conn.commit()
        conn.close()

        kb = InlineKeyboardMarkup([
            [IKB("تایید شارژ ✅", callback_data=f"approve_charge_{charge_id}"),
             IKB("رد شارژ ❌", callback_data=f"reject_charge_{charge_id}")]
        ])
        caption = f"🧾 درخواست شارژ کیف پول\nکاربر: {user.id} (@{user.username or '-'})\nمبلغ: {d['charge_amount']:,} تومان"
        await notify_all_admins_photo(context, photo_file_id, caption, kb, perm="orders")

        await update.message.reply_text("رسید شما دریافت شد و برای بررسی به ادمین ارسال گردید. لطفا منتظر بمانید ⏳")
        clear_state(context)
        return

    await handle_admin_photo_states(update, context, state, photo_file_id)

async def video_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        return
    state = context.user_data.get('state')
    if state == "waiting_support_msg":
        await submit_support_ticket(update, context)
    elif state == "admin_reply_ticket_text" and is_admin(user.id):
        await admin_finish_ticket_reply(update, context)

async def voice_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        return
    state = context.user_data.get('state')
    if state == "waiting_support_msg":
        await submit_support_ticket(update, context)
    elif state == "admin_reply_ticket_text" and is_admin(user.id):
        await admin_finish_ticket_reply(update, context)

async def cb_approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id):
    query = update.callback_query
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order['status'] != "در انتظار تایید":
        conn.close()
        await query.edit_message_caption("این رسید قبلا بررسی شده است.")
        return
    plan_id = order['plan_id']
    cfg = conn.execute("SELECT * FROM plan_configs WHERE plan_id=? AND used=0 LIMIT 1", (plan_id,)).fetchone()
    if not cfg:
        conn.execute("UPDATE orders SET status='ظرفیت تمام' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        await query.edit_message_caption("ظرفیت این پلن تمام شده — نمی‌توان تایید کرد ❌")
        await context.bot.send_message(order['user_id'], "متاسفانه ظرفیت این پلن تمام شده. مبلغ به شما بازگردانده خواهد شد، با پشتیبانی در تماس باشید.")
        return
    expires = (datetime.now() + timedelta(days=get_plan_duration(plan_id))).isoformat()
    conn.execute("UPDATE plan_configs SET used=1, used_by=? WHERE id=?", (order['user_id'], cfg['id']))
    conn.execute("UPDATE orders SET status='تحویل شد', config_id=?, expires_at=? WHERE id=?", (cfg['id'], expires, order_id))
    conn.commit()
    conn.close()
    await send_config_to_user(context, order['user_id'], cfg)
    await context.bot.send_message(order['user_id'], "✅ رسید شما تایید شد و کانفیگ ارسال گردید.")
    await query.edit_message_caption(query.message.caption + "\n\n✅ تایید شد.")

async def cb_reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id):
    query = update.callback_query
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order['status'] != "در انتظار تایید":
        conn.close()
        await query.edit_message_caption("این رسید قبلا بررسی شده است.")
        return
    conn.execute("UPDATE orders SET status='رد شد' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    await context.bot.send_message(order['user_id'], "کاربر گرامی رسید شما رد شد لطفا به پشتیبانی پیام بدهید !")
    await query.edit_message_caption(query.message.caption + "\n\n❌ رد شد.")

async def cb_approve_charge(update: Update, context: ContextTypes.DEFAULT_TYPE, charge_id):
    query = update.callback_query
    conn = get_conn()
    charge = conn.execute("SELECT * FROM wallet_charges WHERE id=?", (charge_id,)).fetchone()
    if not charge or charge['status'] != "در انتظار تایید":
        conn.close()
        await query.edit_message_caption("این درخواست قبلا بررسی شده است.")
        return
    conn.execute("UPDATE wallet_charges SET status='تایید شد' WHERE id=?", (charge_id,))
    conn.commit()
    conn.close()
    change_wallet(charge['user_id'], charge['amount'])
    await context.bot.send_message(charge['user_id'], f"✅ کیف پول شما به مبلغ {charge['amount']:,} تومان شارژ شد.")
    await query.edit_message_caption(query.message.caption + "\n\n✅ تایید شد.")

async def cb_reject_charge(update: Update, context: ContextTypes.DEFAULT_TYPE, charge_id):
    query = update.callback_query
    conn = get_conn()
    charge = conn.execute("SELECT * FROM wallet_charges WHERE id=?", (charge_id,)).fetchone()
    if not charge or charge['status'] != "در انتظار تایید":
        conn.close()
        await query.edit_message_caption("این درخواست قبلا بررسی شده است.")
        return
    conn.execute("UPDATE wallet_charges SET status='رد شد' WHERE id=?", (charge_id,))
    conn.commit()
    conn.close()
    await context.bot.send_message(charge['user_id'], "کاربر گرامی رسید شارژ کیف پول شما رد شد لطفا به پشتیبانی پیام بدهید !")
    await query.edit_message_caption(query.message.caption + "\n\n❌ رد شد.")

async def submit_support_ticket(update, context):
    """پیام کاربر به پشتیبانی رو (متن، عکس، فیلم یا ویس، همراه کپشن/متن، با حفظ کامل فرمت‌بندی
    مثل بولد/اسپویلر/نقل‌قول) ثبت و دقیقاً همون‌طوری که هست برای همه‌ی ادمین‌ها ارسال می‌کنه."""
    user = update.effective_user
    msg = update.message
    if msg.text:
        summary = msg.text
    elif msg.caption:
        summary = msg.caption
    elif msg.voice:
        summary = "[پیام صوتی]"
    elif msg.video:
        summary = "[ویدیو]"
    elif msg.photo:
        summary = "[عکس]"
    elif msg.document:
        summary = "[فایل]"
    else:
        summary = "[پیام]"

    conn = get_conn()
    cur = conn.execute("INSERT INTO tickets(user_id, user_msg, status, created_at) VALUES (?,?,?,?)",
                        (user.id, summary, "باز", datetime.now().isoformat()))
    ticket_id = cur.lastrowid
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup([[IKB("ارسال پاسخ به کاربر ↩️", callback_data=f"reply_ticket_{ticket_id}")]])
    await notify_all_admins_text(context, f"📩 پیام پشتیبانی جدید — تیکت #{ticket_id}\nکاربر: {user.id} (@{user.username or '-'})", perm="support")
    await copy_message_to_admins(context, update.effective_chat.id, msg.message_id, reply_markup=kb, perm="support")

    await update.message.reply_text("پیام شما برای پشتیبانی ارسال شد. منتظر پاسخ باشید ⏳")
    clear_state(context)

async def handle_stateful_text(update, context, state, text) -> bool:
    user_id = update.effective_user.id

    if state == "waiting_support_msg":
        await submit_support_ticket(update, context)
        return True

    if state == "waiting_discount_code":
        d = context.user_data['data']
        row, err = validate_discount(text.strip(), user_id, plan_id=d.get('plan_id'))
        if err:
            await update.message.reply_text(err)
            clear_state(context)
            return True
        d['discount_code'] = row['code']
        if row['percent']:
            d['discount_amount'] = (d['price'] * row['percent']) // 100
        else:
            d['discount_amount'] = row['amount']
        await ask_payment_method(update, context)
        return True

    if state == "waiting_charge_amount":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("لطفا فقط عدد معتبر وارد کنید (مثلا 100000):")
            return True
        context.user_data['data'] = {"charge_amount": int(text)}
        context.user_data['state'] = "waiting_wallet_receipt_photo"
        card_number, card_holder = get_card_info()
        holder_line = f"\n👤 به نام: {card_holder}" if card_holder else ""
        await update.message.reply_text(
            f"لطفا مبلغ {int(text):,} تومان رو به شماره کارت زیر واریز کنید و رسید رو ارسال کنید:\n\n"
            f"💳 {card_number}{holder_line}\n\nلطفا رسید (عکس) رو ارسال کنید 🧾♥️"
        )
        return True

    handled = await handle_admin_text_states(update, context, state, text)
    return handled

async def cb_reply_ticket(update, context, ticket_id):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await qanswer(query, "شما دسترسی ادمین ندارید ❌", show_alert=True)
        return
    context.user_data['state'] = "admin_reply_ticket_text"
    context.user_data['data'] = {"ticket_id": ticket_id}
    await context.bot.send_message(query.message.chat_id, "متن پاسخ برای کاربر رو ارسال کنید:")

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await _callback_router_inner(update, context)
    finally:
        await qanswer(query)

async def _callback_router_inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "check_join":
        await cb_check_join(update, context)
        return

    if is_banned(user_id):
        await query.edit_message_text(f"کاربر محترم شما از طرف ادمین مسدود شده اید ❌\nدلیل: {get_ban_reason(user_id)}")
        return

    if is_maintenance_on() and not is_admin(user_id):
        await qanswer(query, "🔧 ربات موقتاً برای تعمیرات خاموش است. لطفا کمی بعد دوباره تلاش کنید.", show_alert=True)
        return

    if data == "cancel_action":
        clear_state(context)
        await query.edit_message_text("عملیات لغو شد.", reply_markup=None)
        return

    if not is_admin(user_id):
        missing = await get_missing_channels(context, user_id)
        if missing:
            await qanswer(query, "❌ کاربر گرامی، شما هنوز عضو تمام کانال‌های اجباری نشده‌اید. لطفا عضو شوید و دوباره تلاش کنید.", show_alert=True)
            return

    denied = _cb_denied(user_id, data)
    if denied:
        await qanswer(query, denied, show_alert=True)
        return
    if await extra_callback_router(update, context, data):
        return

    if data.startswith("buycat_"):
        await show_plans_in_category(update, context, data[len("buycat_"):], mode="buy")
    elif data.startswith("buyplan_"):
        await cb_select_plan(update, context, int(data.split("_")[1]), is_renew=False)
    elif data.startswith("renewplan_"):
        await cb_select_plan(update, context, int(data.split("_")[1]), is_renew=True)
    elif data.startswith("vieworder_"):
        await cb_view_order(update, context, int(data.split("_")[1]))
    elif data.startswith("hideorder_"):
        await cb_hide_order(update, context, int(data.split("_")[1]))
    elif data == "discount_yes":
        await cb_discount_choice(update, context, True)
    elif data == "discount_no":
        await cb_discount_choice(update, context, False)
    elif data == "pay_wallet":
        await cb_pay_wallet(update, context)
    elif data == "pay_card":
        await cb_pay_card(update, context)
    elif data == "charge_wallet":
        await cb_charge_wallet(update, context)
    elif data.startswith("approve_order_"):
        await cb_approve_order(update, context, int(data.split("_")[2]))
    elif data.startswith("reject_order_"):
        await cb_reject_order(update, context, int(data.split("_")[2]))
    elif data.startswith("approve_charge_"):
        await cb_approve_charge(update, context, int(data.split("_")[2]))
    elif data.startswith("reject_charge_"):
        await cb_reject_charge(update, context, int(data.split("_")[2]))
    elif data.startswith("reply_ticket_"):
        await cb_reply_ticket(update, context, int(data.split("_")[2]))
    else:
        await admin_callback_router(update, context, data)

def admin_only(func):
    async def wrapper(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            if update.callback_query:
                await qanswer(update.callback_query, "شما دسترسی ادمین ندارید ❌", show_alert=True)
            else:
                await update.message.reply_text("شما دسترسی ادمین ندارید ❌")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def owner_only(func):
    async def wrapper(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_owner(user_id):
            if update.callback_query:
                await qanswer(update.callback_query, "این قابلیت فقط برای مالک اصلی ربات است ❌", show_alert=True)
            else:
                await update.message.reply_text("این قابلیت فقط برای مالک اصلی ربات است ❌")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def requires_permission(perm):
    """فقط مالک یا ادمینی که این دسترسی رو داره می‌تونه از این تابع استفاده کنه."""
    def decorator(func):
        async def wrapper(update, context, *args, **kwargs):
            user_id = update.effective_user.id
            if not is_admin(user_id):
                if update.callback_query:
                    await qanswer(update.callback_query, "شما دسترسی ادمین ندارید ❌", show_alert=True)
                else:
                    await update.message.reply_text("شما دسترسی ادمین ندارید ❌")
                return
            if not has_permission(user_id, perm):
                msg = f"⛔️ شما دسترسی «{PERMISSIONS.get(perm, perm)}» رو ندارید.\nاز مالک ربات بخواهید این دسترسی رو بهتون بده."
                if update.callback_query:
                    await qanswer(update.callback_query, msg, show_alert=True)
                else:
                    await update.message.reply_text(msg)
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

@requires_permission("add_plan")
async def admin_add_plan_start(update, context):
    context.user_data['state'] = "admin_new_plan_title"
    context.user_data['data'] = {}
    await update.message.reply_text("عنوان (تایتل) پلن جدید رو ارسال کنید:", reply_markup=cancel_kb())

@admin_only
async def admin_new_plan_finish(update, context, category):
    query = update.callback_query
    d = context.user_data.get('data', {})
    if not d.get('title') or 'price' not in d or 'duration' not in d:
        await query.edit_message_text("خطا: اطلاعات پلن ناقص است، لطفا دوباره از منوی «➕ افزودن پلن» شروع کنید.")
        clear_state(context)
        return
    conn = get_conn()
    cur = conn.execute("INSERT INTO plans(title, price, duration_days, is_test, category) VALUES (?,?,?,0,?)",
                        (d['title'], d['price'], d['duration'], category))
    new_plan_id = cur.lastrowid
    conn.commit()
    conn.close()
    await query.edit_message_text(
        f"✅ پلن «{d['title']}» ({CATEGORY_LABELS.get(category, category)}) با قیمت {d['price']:,} تومان و "
        f"{d['duration']} روز اعتبار ساخته شد.\n\nحالا کانفیگ‌های این پلن رو یکی‌یکی وارد کنید:"
    )
    context.user_data['data'] = {"edit_plan_id": new_plan_id, "bulk_add": True, "bulk_count": 0}
    await prompt_next_bulk_config(update, context)

async def prompt_next_bulk_config(update, context):
    d = context.user_data.get('data', {})
    count = d.get('bulk_count', 0)
    context.user_data['state'] = "admin_bulkcfg_choose"
    kb = InlineKeyboardMarkup([
        [IKB("متنی 📝", callback_data="cfgtype_text"),
         IKB("عکس + کپشن 🖼", callback_data="cfgtype_photo")],
        [IKB(f"پایان (تعداد اضافه‌شده: {count}) ✅", callback_data="bulkcfg_done")],
    ])
    text = "کانفیگ بعدی رو وارد کنید یا پایان بدید:"
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)

async def cb_bulkcfg_done(update, context):
    d = context.user_data.get('data', {})
    count = d.get('bulk_count', 0)
    clear_state(context)
    await update.callback_query.edit_message_text(f"✅ تمام. {count} کانفیگ به این پلن اضافه شد.")

@requires_permission("edit_plan")
async def admin_edit_plan_start(update, context):
    kb = [[IKB(CATEGORY_LABELS[c], callback_data=f"editcat_{c}")] for c in PLAN_CATEGORIES]
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await update.message.reply_text("ویرایش پلن — ابتدا دسته رو انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_edit_plan_list_by_category(update, context, category):
    query = update.callback_query
    conn = get_conn()
    plans = conn.execute("SELECT * FROM plans WHERE is_test=0 AND COALESCE(is_coin,0)=0 AND category=?", (category,)).fetchall()
    conn.close()
    label = CATEGORY_LABELS.get(category, category)
    if not plans:
        await query.edit_message_text(f"هیچ پلنی در دسته «{label}» ثبت نشده است.", reply_markup=cancel_kb())
        return
    kb = [[IKB(p['title'], callback_data=f"editplan_sel_{p['id']}")] for p in plans]
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await query.edit_message_text(f"{label}\nکدام پلن رو می‌خواهید ویرایش کنید؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_edit_plan_menu(update, context, plan_id):
    query = update.callback_query
    context.user_data['data'] = {"edit_plan_id": plan_id}
    kb = InlineKeyboardMarkup([
        [IKB("تغییر عنوان ✏️", callback_data=f"editfield_title_{plan_id}")],
        [IKB("تغییر قیمت 💵", callback_data=f"editfield_price_{plan_id}")],
        [IKB("تغییر مدت اعتبار 📅", callback_data=f"editfield_duration_{plan_id}")],
        [IKB("تغییر دسته 🏷", callback_data=f"editfield_category_{plan_id}")],
        [IKB("افزودن کانفیگ به این پلن ➕", callback_data=f"editfield_addcfg_{plan_id}")],
        [IKB("مشاهده/حذف کانفیگ‌های این پلن 📋", callback_data=f"editfield_listcfg_{plan_id}")],
        [IKB("لغو ❌", callback_data="cancel_action")],
    ])
    await query.edit_message_text("چه چیزی را می‌خواهید ویرایش کنید؟", reply_markup=kb)

@admin_only
async def admin_edit_field_start(update, context, field, plan_id):
    query = update.callback_query
    context.user_data['data'] = {"edit_plan_id": plan_id, "edit_field": field}
    prompts = {
        "title": "عنوان جدید را ارسال کنید:",
        "price": "قیمت جدید را به تومان ارسال کنید (فقط عدد):",
        "duration": "مدت اعتبار جدید را به روز ارسال کنید (فقط عدد):",
    }
    if field == "addcfg":
        context.user_data['state'] = "admin_addcfg_to_plan_type"
        kb = InlineKeyboardMarkup([
            [IKB("متنی 📝", callback_data="cfgtype_text"),
             IKB("عکس + کپشن 🖼", callback_data="cfgtype_photo")]
        ])
        await query.edit_message_text("نوع کانفیگ رو انتخاب کنید:", reply_markup=kb)
        return
    if field == "listcfg":
        await show_plan_configs_list(update, context, plan_id)
        return
    if field == "category":
        kb = InlineKeyboardMarkup([[IKB(CATEGORY_LABELS[c], callback_data=f"setcat_{plan_id}_{c}")] for c in PLAN_CATEGORIES])
        await query.edit_message_text("دسته جدید این پلن رو انتخاب کنید:", reply_markup=kb)
        return
    context.user_data['state'] = f"admin_edit_{field}"
    await query.edit_message_text(prompts[field], reply_markup=cancel_kb())

async def show_plan_configs_list(update, context, plan_id):
    query = update.callback_query
    conn = get_conn()
    cfgs = conn.execute("SELECT * FROM plan_configs WHERE plan_id=? AND used=0", (plan_id,)).fetchall()
    conn.close()
    if not cfgs:
        await query.edit_message_text("کانفیگ فعالی (استفاده‌نشده) برای این پلن وجود ندارد.", reply_markup=cancel_kb())
        return
    lines = [f"تعداد کانفیگ‌های موجود: {len(cfgs)}\n"]
    kb = []
    for c in cfgs:
        if c['content_type'] == 'text':
            preview = (c['text_content'] or "").strip().replace("\n", " ")
            if len(preview) > 60:
                preview = preview[:60] + "…"
            lines.append(f"#{c['id']} 📝 — {preview}")
            kb.append([IKB(f"حذف #{c['id']} 🗑", callback_data=f"delcfg_{c['id']}_{plan_id}")])
        else:
            lines.append(f"#{c['id']} 🖼 — عکس (برای دیدنش دکمه‌ی مشاهده رو بزن)")
            kb.append([
                IKB(f"مشاهده #{c['id']} 👁", callback_data=f"viewcfg_{c['id']}"),
                IKB(f"حذف #{c['id']} 🗑", callback_data=f"delcfg_{c['id']}_{plan_id}"),
            ])
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_view_single_config(update, context, cfg_id):
    query = update.callback_query
    conn = get_conn()
    c = conn.execute("SELECT * FROM plan_configs WHERE id=?", (cfg_id,)).fetchone()
    conn.close()
    if not c:
        await qanswer(query, "این کانفیگ دیگر وجود ندارد.", show_alert=True)
        return
    await qanswer(query, )
    if c['content_type'] == 'photo':
        await context.bot.send_photo(update.effective_user.id, c['photo_file_id'], caption=c['caption'] or "")
    else:
        await context.bot.send_message(update.effective_user.id, c['text_content'])

@admin_only
async def admin_delete_single_config(update, context, cfg_id, plan_id):
    query = update.callback_query
    conn = get_conn()
    conn.execute("DELETE FROM plan_configs WHERE id=?", (cfg_id,))
    conn.commit()
    conn.close()
    await qanswer(query, "کانفیگ حذف شد ✅", show_alert=True)
    await show_plan_configs_list(update, context, plan_id)

@admin_only
async def admin_set_plan_category(update, context, plan_id, category):
    query = update.callback_query
    if category not in PLAN_CATEGORIES:
        await qanswer(query, "دسته نامعتبر است ❌", show_alert=True)
        return
    conn = get_conn()
    conn.execute("UPDATE plans SET category=? WHERE id=?", (category, plan_id))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"✅ دسته‌ی این پلن به «{CATEGORY_LABELS.get(category, category)}» تغییر کرد.")

@requires_permission("del_plan")
async def admin_del_plan_start(update, context):
    kb = [[IKB(CATEGORY_LABELS[c], callback_data=f"delcat_{c}")] for c in PLAN_CATEGORIES]
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await update.message.reply_text("حذف پلن — ابتدا دسته رو انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_del_plan_list_by_category(update, context, category):
    query = update.callback_query
    conn = get_conn()
    plans = conn.execute("SELECT * FROM plans WHERE is_test=0 AND COALESCE(is_coin,0)=0 AND category=?", (category,)).fetchall()
    conn.close()
    label = CATEGORY_LABELS.get(category, category)
    if not plans:
        await query.edit_message_text(f"هیچ پلنی در دسته «{label}» ثبت نشده است.", reply_markup=cancel_kb())
        return
    kb = [[IKB(f"{p['title']} 🗑", callback_data=f"delplan_confirm_{p['id']}")] for p in plans]
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await query.edit_message_text(f"{label}\nکدام پلن حذف شود؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_del_plan_confirm(update, context, plan_id):
    query = update.callback_query
    conn = get_conn()
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    conn.execute("DELETE FROM plan_configs WHERE plan_id=?", (plan_id,))
    conn.commit()
    conn.close()
    await query.edit_message_text("✅ پلن و تمام کانفیگ‌های مرتبط با آن حذف شد. (ظرفیت به‌طور خودکار صفر شد)")

@requires_permission("add_test")
async def admin_add_test_start(update, context):
    context.user_data['state'] = "admin_addtest_type"
    context.user_data['data'] = {}
    kb = InlineKeyboardMarkup([
        [IKB("متنی 📝", callback_data="testtype_text"),
         IKB("عکس + کپشن 🖼", callback_data="testtype_photo")]
    ])
    await update.message.reply_text("نوع کانفیگ تست رو انتخاب کنید:", reply_markup=kb)

@requires_permission("list_test")
async def admin_list_test_configs(update, context):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM test_configs ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("هیچ کانفیگ تستی ثبت نشده است.")
        return
    lines = [f"🎁 کانفیگ‌های تست ({len(rows)} مورد):\n"]
    for r in rows:
        status = "✅ استفاده‌نشده" if not r['used'] else f"❌ استفاده‌شده (توسط {r['used_by']})"
        kind = "متنی 📝" if r['content_type'] == 'text' else "عکس 🖼"
        lines.append(f"#{r['id']} — {kind} — {status}")
    await update.message.reply_text("\n".join(lines))

@requires_permission("del_test")
async def admin_delete_test_start(update, context):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM test_configs WHERE used=0 ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("کانفیگ تست فعال (استفاده‌نشده)‌ای برای حذف وجود ندارد.")
        return
    kb = []
    for r in rows:
        kind = "متنی" if r['content_type'] == 'text' else "عکس"
        kb.append([IKB(f"#{r['id']} ({kind}) 🗑", callback_data=f"deltest_{r['id']}")])
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدام کانفیگ تست حذف شود؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_delete_test_do(update, context, cfg_id):
    query = update.callback_query
    conn = get_conn()
    conn.execute("DELETE FROM test_configs WHERE id=?", (cfg_id,))
    conn.commit()
    conn.close()
    await qanswer(query, "کانفیگ تست حذف شد ✅", show_alert=True)
    conn = get_conn()
    rows = conn.execute("SELECT * FROM test_configs WHERE used=0 ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        await query.edit_message_text("کانفیگ تست فعال دیگری باقی نمانده است.")
        return
    kb = []
    for r in rows:
        kind = "متنی" if r['content_type'] == 'text' else "عکس"
        kb.append([IKB(f"#{r['id']} ({kind}) 🗑", callback_data=f"deltest_{r['id']}")])
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await query.edit_message_text("کدام کانفیگ تست حذف شود؟", reply_markup=InlineKeyboardMarkup(kb))

@requires_permission("edit_test")
async def admin_edit_test_start(update, context):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM test_configs WHERE used=0 ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("کانفیگ تست فعال (استفاده‌نشده)‌ای برای ویرایش وجود ندارد.")
        return
    kb = []
    for r in rows:
        kind = "متنی" if r['content_type'] == 'text' else "عکس"
        kb.append([IKB(f"#{r['id']} ({kind}) ✏️", callback_data=f"edittest_sel_{r['id']}")])
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدام کانفیگ تست ویرایش شود؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_edit_test_select(update, context, cfg_id):
    query = update.callback_query
    conn = get_conn()
    row = conn.execute("SELECT * FROM test_configs WHERE id=?", (cfg_id,)).fetchone()
    conn.close()
    if not row:
        await query.edit_message_text("این کانفیگ تست دیگر وجود ندارد.")
        return
    context.user_data['data'] = {"edit_test_id": cfg_id}
    if row['content_type'] == 'text':
        context.user_data['state'] = "admin_edittest_text"
        await query.edit_message_text("متن جدید کانفیگ تست رو ارسال کنید:")
    else:
        context.user_data['state'] = "admin_edittest_photo"
        await query.edit_message_text("عکس جدید + کپشن رو ارسال کنید (کپشن رو داخل خود عکس بنویسید):")

@requires_permission("list_admins")
async def admin_list_admins(update, context):
    conn = get_conn()
    admins = conn.execute("SELECT * FROM admins").fetchall()
    owners = {r['user_id'] for r in conn.execute("SELECT user_id FROM owners").fetchall()}
    conn.close()
    if not admins:
        await update.message.reply_text("هیچ ادمینی ثبت نشده است.")
        return
    lines = ["👤 لیست ادمین‌های ربات:\n"]
    for a in admins:
        role = "👑 مالک اصلی" if a['user_id'] in owners else "🛠 ادمین"
        lines.append(f"• {a['user_id']} — {role}")
    await update.message.reply_text("\n".join(lines))

@requires_permission("change_card")
async def admin_change_card_start(update, context):
    context.user_data['state'] = "admin_card_number"
    context.user_data['data'] = {}
    number, holder = get_card_info()
    await update.message.reply_text(
        f"شماره کارت فعلی: {number}\nبه نام: {holder or '—'}\n\nشماره کارت جدید رو ارسال کنید:",
        reply_markup=cancel_kb()
    )

@requires_permission("welcome")
async def admin_change_welcome_start(update, context):
    context.user_data['state'] = "admin_welcome_message"
    current = get_custom_welcome_message()
    bot_name = context.bot.first_name
    preview = current if current else build_welcome_message(bot_name)
    await update.message.reply_text(
        f"پیام خوش‌آمدگویی فعلی:\n\n{preview}\n\n"
        "متن جدید رو ارسال کنید (می‌تونید از {نام} برای درج خودکار اسم ربات استفاده کنید).\n"
        "برای بازگشت به پیام پیش‌فرض، کلمه «پیش‌فرض» رو ارسال کنید:",
        reply_markup=cancel_kb()
    )

@requires_permission("maintenance")
async def admin_maintenance_toggle_start(update, context):
    if is_maintenance_on():
        set_maintenance(False)
        await broadcast_maintenance(context, "✅ ربات دوباره فعال شد و می‌توانید از آن استفاده کنید.")
        await update.message.reply_text("✅ ربات روشن شد و پیام اطلاع‌رسانی برای کاربران ارسال شد.")
    else:
        context.user_data['state'] = "admin_maintenance_off_text"
        context.user_data['data'] = {}
        await update.message.reply_text(
            "متنی که می‌خواهید قبل از خاموش شدن ربات برای همه کاربران ارسال شود رو بنویسید:",
            reply_markup=cancel_kb()
        )

async def broadcast_maintenance(context, text):
    conn = get_conn()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    for u in users:
        try:
            await context.bot.send_message(u['user_id'], text)
        except TelegramError:
            pass

@owner_only
async def admin_transfer_ownership_start(update, context):
    conn = get_conn()
    admins = conn.execute("SELECT * FROM admins").fetchall()
    conn.close()
    kb = []
    for a in admins:
        if is_owner(a['user_id']):
            continue
        kb.append([IKB(f"{a['user_id']} ♻️", callback_data=f"transferown_{a['user_id']}")])
    if not kb:
        await update.message.reply_text("ادمین دیگری برای انتقال مالکیت به او وجود ندارد. اول یک ادمین اضافه کنید.")
        return
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await update.message.reply_text(
        "⚠️ توجه: با انتقال مالکیت، شما ادمین معمولی می‌شوید و دیگر دسترسی مالک اصلی را نخواهید داشت.\n\nمالکیت به کدام ادمین منتقل شود؟",
        reply_markup=InlineKeyboardMarkup(kb)
    )

@owner_only
async def admin_transfer_ownership_confirm(update, context, target_id):
    query = update.callback_query
    kb = InlineKeyboardMarkup([
        [IKB("بله، مطمئنم ✅", callback_data=f"transferownok_{target_id}")],
        [IKB("لغو ❌", callback_data="cancel_action")],
    ])
    await query.edit_message_text(
        f"⚠️ آیا مطمئنید می‌خواهید مالکیت ربات رو به کاربر {target_id} منتقل کنید؟\nاین کار قابل بازگشت نیست مگر اینکه مالک جدید دوباره شما را مالک کند.",
        reply_markup=kb
    )

@owner_only
async def admin_transfer_ownership_do(update, context, target_id):
    query = update.callback_query
    old_owner_id = update.effective_user.id
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (target_id,))
    conn.execute("INSERT OR IGNORE INTO owners(user_id) VALUES (?)", (target_id,))
    conn.execute("DELETE FROM owners WHERE user_id=?", (old_owner_id,))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"✅ مالکیت ربات به کاربر {target_id} منتقل شد. شما اکنون ادمین معمولی هستید.")
    try:
        await context.bot.send_message(target_id, "🎉 مالکیت ربات به شما منتقل شد. شما اکنون مالک اصلی ربات هستید.")
    except TelegramError:
        pass

@owner_only
async def admin_wheel_toggle(update, context):
    if is_wheel_enabled():
        set_wheel_enabled(False)
        await update.message.reply_text("🎡 گردونه شانس خاموش شد. دکمه گردونه دیگه برای کاربرا نمایش داده نمی‌شه.", reply_markup=admin_menu_kb(update.effective_user.id))
    else:
        set_wheel_enabled(True)
        prizes = get_wheel_prizes()
        if not prizes:
            await update.message.reply_text("✅ گردونه روشن شد. حالا باید جایزه‌هاش رو تنظیم کنی:")
            await start_wheel_prize_wizard(update, context)
        else:
            await update.message.reply_text("✅ گردونه شانس روشن شد و برای کاربرا نمایش داده می‌شه.", reply_markup=admin_menu_kb(update.effective_user.id))

@owner_only
async def admin_wheel_manage(update, context):
    prizes = get_wheel_prizes()
    settings = get_wheel_settings()
    status = "روشن ✅" if settings['enabled'] else "خاموش ❌"
    lines = [f"وضعیت گردونه: {status}", f"فاصله زمانی هر کاربر: هر {settings['cooldown_hours']} ساعت", f"تعداد جایزه‌ها: {len(prizes)}"]
    kb = [
        [IKB("افزودن جایزه ➕", callback_data="wheel_addprize")],
        [IKB("لیست/حذف جایزه‌ها 📋", callback_data="wheel_listprizes")],
        [IKB("تغییر فاصله زمانی ⏱", callback_data="wheel_setcooldown")],
        [IKB("بستن ❌", callback_data="cancel_action")],
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def start_wheel_prize_wizard(update, context):
    context.user_data['state'] = "wheel_addprize_type"
    context.user_data['data'] = {}
    kb = InlineKeyboardMarkup([
        [IKB(WHEEL_PRIZE_LABELS["discount"], callback_data="wheeltype_discount")],
        [IKB(WHEEL_PRIZE_LABELS["config"], callback_data="wheeltype_config")],
        [IKB(WHEEL_PRIZE_LABELS["respin"], callback_data="wheeltype_respin")],
        [IKB(WHEEL_PRIZE_LABELS["none"], callback_data="wheeltype_none")],
        [IKB("پایان ✅", callback_data="wheel_wizard_done")],
    ])
    text = "نوع جایزه بعدی رو انتخاب کن یا پایان بده:"
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)

@owner_only
async def cb_wheel_choose_type(update, context, ptype):
    query = update.callback_query
    d = context.user_data.setdefault('data', {})
    d['prize_type'] = ptype
    if ptype == "discount":
        context.user_data['state'] = "wheel_discount_percent"
        await query.edit_message_text("چند درصد تخفیف بده؟ (فقط عدد، مثلا 20):")
    elif ptype == "config":
        context.user_data['state'] = "wheel_config_gb"
        await query.edit_message_text("این کانفیگ رایگان چند گیگ باشه؟ (فقط عدد، مثلا 5):")
    else:
        context.user_data['state'] = "wheel_weight"
        await query.edit_message_text("شانس (احتمال) این جایزه چند درصد باشه؟ (فقط عدد، مثلا 30):")

@owner_only
async def cb_wheel_wizard_done(update, context):
    clear_state(context)
    prizes = get_wheel_prizes()
    total = sum(p['weight'] for p in prizes)
    lines = [f"✅ تنظیم جایزه‌ها تمام شد. تعداد کل: {len(prizes)}"]
    if total != 100:
        lines.append(f"⚠️ توجه: مجموع شانس‌ها {total}% است (بهتره مجموعاً ۱۰۰٪ باشه، ولی مشکلی هم نداره).")
    await update.callback_query.edit_message_text("\n".join(lines))

@owner_only
async def cb_wheel_list_prizes(update, context):
    query = update.callback_query
    prizes = get_wheel_prizes()
    if not prizes:
        await query.edit_message_text("هیچ جایزه‌ای تعریف نشده.", reply_markup=cancel_kb())
        return
    lines = ["📋 جایزه‌های گردونه:\n"]
    kb = []
    for p in prizes:
        if p['prize_type'] == "discount":
            desc = f"{WHEEL_PRIZE_LABELS['discount']} {p['discount_percent']}% ({p['discount_hours']} ساعته)"
        elif p['prize_type'] == "config":
            conn = get_conn()
            cnt = conn.execute("SELECT COUNT(*) c FROM wheel_prize_configs WHERE prize_id=? AND used=0", (p['id'],)).fetchone()['c']
            conn.close()
            desc = f"{WHEEL_PRIZE_LABELS['config']} {p['config_gb']:g} گیگ (موجودی: {cnt})"
        else:
            desc = WHEEL_PRIZE_LABELS.get(p['prize_type'], p['prize_type'])
        lines.append(f"#{p['id']} — {desc} — شانس: {p['weight']}%")
        row = [IKB(f"حذف #{p['id']} 🗑", callback_data=f"wheeldelprize_{p['id']}")]
        if p['prize_type'] == "config":
            row.append(IKB(f"افزودن کانفیگ #{p['id']} ➕", callback_data=f"wheeladdcfg_{p['id']}"))
        kb.append(row)
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

@owner_only
async def cb_wheel_delete_prize(update, context, prize_id):
    delete_wheel_prize(prize_id)
    await qanswer(update.callback_query, "جایزه حذف شد ✅", show_alert=True)
    await cb_wheel_list_prizes(update, context)

@owner_only
async def cb_wheel_addcfg_start(update, context, prize_id):
    query = update.callback_query
    context.user_data['data'] = {"wheel_prize_id": prize_id}
    kb = InlineKeyboardMarkup([
        [IKB("متنی 📝", callback_data="wheelcfgtype_text"),
         IKB("عکس + کپشن 🖼", callback_data="wheelcfgtype_photo")],
        [IKB("پایان ✅", callback_data="cancel_action")],
    ])
    await query.edit_message_text("کانفیگ این جایزه رو وارد کنید (نوعش رو انتخاب کنید):", reply_markup=kb)

@owner_only
async def cb_wheel_setcooldown_start(update, context):
    context.user_data['state'] = "wheel_cooldown_hours"
    await update.callback_query.edit_message_text("هر چند ساعت یک‌بار هر کاربر بتونه گردونه بزنه؟ (فقط عدد، مثلا 24):")

async def hourly_backup_job(context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(DB_PATH):
        return
    conn = get_conn()
    owners = conn.execute("SELECT user_id FROM owners").fetchall()
    conn.close()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    snapshot_path = f"{DB_PATH}.autobackup_tmp"
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(snapshot_path)
        src.backup(dst)
        dst.close()
        src.close()
    except Exception as e:
        logger.error(f"ساخت اسنپ‌شات بکاپ خودکار ناموفق بود: {e}")
        for o in owners:
            try:
                await context.bot.send_message(o['user_id'], f"⚠️ بکاپ خودکار ساعت {ts} با خطا مواجه شد و ارسال نشد: {e}")
            except Exception:
                pass
        return

    if not owners:
        os.remove(snapshot_path)
        return

    for o in owners:
        try:
            with open(snapshot_path, "rb") as f:
                await context.bot.send_document(
                    o['user_id'], document=f, filename=f"autobackup_{file_ts}.db",
                    caption=f"💾 بکاپ خودکار — {ts}"
                )
        except Exception as e:
            logger.warning(f"ارسال بکاپ خودکار به مالک {o['user_id']} ناموفق بود: {e}")
            try:
                await context.bot.send_message(o['user_id'], f"⚠️ ارسال فایل بکاپ خودکار ساعت {ts} ناموفق بود: {e}")
            except Exception:
                pass
    try:
        os.remove(snapshot_path)
    except OSError:
        pass

@requires_permission("new_discount")
async def admin_add_discount_start(update, context):
    context.user_data['state'] = "admin_discount_code"
    context.user_data['data'] = {}
    await update.message.reply_text("کد تخفیف رو ارسال کنید (مثلا OFF10):", reply_markup=cancel_kb())

@admin_only
async def admin_discount_scope_all(update, context):
    await finalize_discount_code(update, context, "all", None)

@admin_only
async def admin_discount_scope_category(update, context, category):
    await finalize_discount_code(update, context, "category", category)

@admin_only
async def admin_discount_scope_plan_list(update, context):
    query = update.callback_query
    conn = get_conn()
    plans = conn.execute("SELECT id, title, category FROM plans WHERE is_test=0 AND COALESCE(is_coin,0)=0 ORDER BY category, title").fetchall()
    conn.close()
    if not plans:
        await qanswer(query, "هیچ پلنی ثبت نشده است.", show_alert=True)
        return
    kb = [[IKB(f"{p['title']} ({CATEGORY_LABELS.get(p['category'], p['category'])})", callback_data=f"discscopeplan_{p['id']}")] for p in plans]
    kb.append([IKB("↩️ برگشت", callback_data="discscope_back")])
    await query.edit_message_text("این کد تخفیف فقط برای کدوم پلن معتبر باشه؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_discount_scope_plan_pick(update, context, plan_id):
    await finalize_discount_code(update, context, "plan", str(plan_id))

async def finalize_discount_code(update, context, scope, value):
    query = update.callback_query
    d = context.user_data.get('data', {})
    if not d.get('code'):
        await qanswer(query, "این عملیات منقضی شده، دوباره از منو شروع کنید.", show_alert=True)
        return
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO discount_codes(code, amount, expires_at, max_uses, used_count, active, target_scope, target_value) VALUES (?,?,?,?,0,1,?,?)",
        (d['code'], d['amount'], d['expires_at'], d['max_uses'], scope, value)
    )
    conn.commit()
    conn.close()
    if scope == "all":
        scope_txt = "همه‌ی پلن‌ها"
    elif scope == "category":
        scope_txt = f"فقط دسته‌ی «{CATEGORY_LABELS.get(value, value)}»"
    else:
        conn = get_conn()
        p = conn.execute("SELECT title FROM plans WHERE id=?", (value,)).fetchone()
        conn.close()
        scope_txt = f"فقط پلن «{p['title'] if p else value}»"
    await qanswer(query, "✅ کد تخفیف ساخته شد")
    await query.edit_message_text(f"✅ کد تخفیف «{d['code']}» ساخته شد.\nمبلغ کاهش: {d['amount']:,} تومان\nدامنه: {scope_txt}")
    clear_state(context)

@requires_permission("revoke_discount")
async def admin_revoke_discount_start(update, context):
    conn = get_conn()
    codes = conn.execute("SELECT * FROM discount_codes WHERE active=1").fetchall()
    conn.close()
    if not codes:
        await update.message.reply_text("هیچ کد تخفیف فعالی وجود ندارد.")
        return
    kb = []
    for c in codes:
        used_txt = f"{c['used_count']}/{c['max_uses'] if c['max_uses'] != -1 else '∞'}"
        kb.append([IKB(f"باطل کردن {c['code']} (مصرف: {used_txt}) ❌", callback_data=f"revokecode_{c['code']}")])
    kb.append([IKB("بستن", callback_data="cancel_action")])
    await update.message.reply_text("کدام کد تخفیف باطل شود؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_revoke_discount_do(update, context, code):
    query = update.callback_query
    conn = get_conn()
    conn.execute("UPDATE discount_codes SET active=0 WHERE code=?", (code,))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"✅ کد تخفیف «{code}» باطل شد.")

@requires_permission("users")
async def admin_show_users(update, context):
    conn = get_conn()
    users = conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()
    conn.close()
    if not users:
        await update.message.reply_text("هیچ کاربری ثبت نشده است.")
        return
    await update.message.reply_text(f"👥 تعداد کل کاربران: {len(users)}")
    for u in users:
        conn = get_conn()
        orders = conn.execute("SELECT * FROM orders WHERE user_id=? AND status='تحویل شد'", (u['user_id'],)).fetchall()
        conn.close()
        status = "🚫 مسدود" if u['banned'] else "✅ فعال"
        text = (f"آیدی: {u['user_id']}\nیوزرنیم: @{u['username'] or '-'}\n"
                f"کیف پول: {u['wallet']:,} تومان\nوضعیت: {status}\nتعداد خرید موفق: {len(orders)}")
        kb_rows = []
        if orders:
            kb_rows.append([IKB("مشاهده کانفیگ‌های خریداری‌شده 📦", callback_data=f"viewuserorders_{u['user_id']}")])
        kb_rows.append([IKB("مشاهده کاربر 💬", url=f"tg://user?id={u['user_id']}")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))

@admin_only
async def admin_view_user_orders(update, context, target_id):
    query = update.callback_query
    conn = get_conn()
    orders = conn.execute("SELECT * FROM orders WHERE user_id=? AND status='تحویل شد' ORDER BY id DESC", (target_id,)).fetchall()
    conn.close()
    if not orders:
        await qanswer(query, "این کاربر کانفیگ خریداری‌شده‌ای ندارد.", show_alert=True)
        return
    await qanswer(query, )
    await context.bot.send_message(query.message.chat_id, f"📦 کانفیگ‌های خریداری‌شده کاربر {target_id}:")
    for o in orders:
        expires = datetime.fromisoformat(o['expires_at']) if o['expires_at'] else None
        remain_txt = "-"
        if expires:
            remaining = expires - datetime.now()
            remain_txt = f"{remaining.days} روز مانده" if remaining.total_seconds() > 0 else "منقضی شده"
        kb = None
        if o['config_id']:
            kb = InlineKeyboardMarkup([[IKB("مشاهده کانفیگ 👁", callback_data=f"adminvieworder_{o['id']}")]])
        await context.bot.send_message(
            query.message.chat_id,
            f"• {o['plan_title']} | {_order_price_text(o)} | {remain_txt}",
            reply_markup=kb
        )

async def admin_view_order_config(update, context, order_id):
    """محتوای واقعی کانفیگِ یک سفارش رو (عکس/متن/هرچی) برای ادمین نشون می‌ده — همون چیزی که کاربر گرفته."""
    query = update.callback_query
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    cfg = conn.execute("SELECT * FROM plan_configs WHERE id=?", (order['config_id'],)).fetchone() if order and order['config_id'] else None
    conn.close()
    if not order or not cfg:
        await qanswer(query, "کانفیگ این سفارش دیگر در دسترس نیست.", show_alert=True)
        return
    await qanswer(query, )
    if cfg['content_type'] == 'photo':
        await context.bot.send_photo(query.message.chat_id, cfg['photo_file_id'], caption=cfg['caption'] or "")
    else:
        await context.bot.send_message(query.message.chat_id, cfg['text_content'])

@requires_permission("direct_msg")
async def admin_direct_start(update, context):
    context.user_data['state'] = "admin_direct_userid"
    context.user_data['data'] = {}
    await update.message.reply_text("آیدی عددی کاربر مورد نظر رو ارسال کنید:", reply_markup=cancel_kb())

@requires_permission("broadcast")
async def admin_broadcast_start(update, context):
    context.user_data['state'] = "admin_broadcast_text"
    context.user_data['data'] = {}
    await update.message.reply_text("متن پیام همگانی رو ارسال کنید (برای همه کاربران ارسال می‌شود):", reply_markup=cancel_kb())

@requires_permission("charge_wallet")
async def admin_chargewallet_start(update, context):
    context.user_data['state'] = "admin_chargewallet_userid"
    context.user_data['data'] = {}
    await update.message.reply_text("آیدی عددی کاربر مورد نظر رو ارسال کنید:", reply_markup=cancel_kb())

@requires_permission("dec_wallet")
async def admin_decrease_wallet_start(update, context):
    context.user_data['state'] = "admin_decwallet_userid"
    context.user_data['data'] = {}
    await update.message.reply_text("آیدی عددی کاربری که می‌خواهید از کیف پولش کسر کنید رو ارسال کنید:", reply_markup=cancel_kb())

@requires_permission("ban")
async def admin_ban_start(update, context):
    context.user_data['state'] = "admin_ban_userid"
    context.user_data['data'] = {}
    await update.message.reply_text("آیدی عددی کاربری که می‌خواهید مسدود کنید رو ارسال کنید:", reply_markup=cancel_kb())

@requires_permission("unban")
async def admin_unban_list(update, context):
    conn = get_conn()
    banned = conn.execute("SELECT * FROM users WHERE banned=1").fetchall()
    conn.close()
    if not banned:
        await update.message.reply_text("هیچ کاربر مسدودی وجود ندارد.")
        return
    kb = [[IKB(f"رفع مسدودی {u['user_id']} ✅", callback_data=f"unban_{u['user_id']}")] for u in banned]
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await update.message.reply_text("کاربران مسدود:", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_unban_do(update, context, target_id):
    query = update.callback_query
    conn = get_conn()
    conn.execute("UPDATE users SET banned=0, ban_reason=NULL WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    await context.bot.send_message(target_id, "حالا می‌توانید از ربات استفاده کنید✅")
    await qanswer(query, "رفع مسدودی انجام شد ✅", show_alert=True)
    conn = get_conn()
    banned = conn.execute("SELECT * FROM users WHERE banned=1").fetchall()
    conn.close()
    if not banned:
        await query.edit_message_text("هیچ کاربر مسدودی باقی نمانده است.")
        return
    kb = [[IKB(f"رفع مسدودی {u['user_id']} ✅", callback_data=f"unban_{u['user_id']}")] for u in banned]
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await query.edit_message_text("کاربران مسدود:", reply_markup=InlineKeyboardMarkup(kb))

@requires_permission("add_admin")
async def admin_add_admin_start(update, context):
    conn = get_conn()
    users = conn.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT 30").fetchall()
    admin_ids = {r['user_id'] for r in conn.execute("SELECT user_id FROM admins").fetchall()}
    conn.close()
    candidates = [u for u in users if u['user_id'] not in admin_ids]
    kb = []
    for u in candidates:
        label = f"@{u['username']}" if u['username'] else str(u['user_id'])
        kb.append([IKB(f"{label} 👤", callback_data=f"selnewadmin_{u['user_id']}")])
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    text = "یکی از کاربران رو برای ادمین کردن انتخاب کنید،" if candidates else "کاربر تازه‌ای (که ادمین نباشه) پیدا نشد."
    text += "\nیا آیدی عددی رو مستقیم بفرستید:"
    context.user_data['state'] = "admin_add_admin_id"
    context.user_data['data'] = {}
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

@owner_only
async def admin_edit_permissions_start(update, context):
    conn = get_conn()
    admins = conn.execute("SELECT * FROM admins").fetchall()
    conn.close()
    kb = []
    for a in admins:
        if is_owner(a['user_id']):
            continue
        kb.append([IKB(f"{a['user_id']} 🔑", callback_data=f"editadminperm_{a['user_id']}")])
    if not kb:
        await update.message.reply_text("ادمین دیگری (غیر از مالک) برای ویرایش دسترسی وجود ندارد.")
        return
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await update.message.reply_text("دسترسی‌های کدام ادمین رو می‌خواهید ویرایش کنید؟", reply_markup=InlineKeyboardMarkup(kb))

@owner_only
async def admin_del_admin_start(update, context):
    conn = get_conn()
    admins = conn.execute("SELECT * FROM admins").fetchall()
    conn.close()
    kb = []
    for a in admins:
        if is_owner(a['user_id']):
            continue
        kb.append([IKB(f"حذف {a['user_id']} 🗑", callback_data=f"removeadmin_{a['user_id']}")])
    if not kb:
        await update.message.reply_text("ادمین دیگری (غیر از مالک اصلی) برای حذف وجود ندارد.")
        return
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدام ادمین حذف شود؟", reply_markup=InlineKeyboardMarkup(kb))

@owner_only
async def admin_del_admin_do(update, context, target_id):
    query = update.callback_query
    if is_owner(target_id):
        await qanswer(query, "امکان حذف مالک اصلی وجود ندارد ❌", show_alert=True)
        return
    conn = get_conn()
    conn.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
    conn.execute("DELETE FROM admin_permissions WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()
    try:
        await context.bot.send_message(target_id, "دسترسی ادمین شما توسط مالک ربات حذف شد.")
    except TelegramError:
        pass
    await query.edit_message_text(f"✅ ادمین {target_id} حذف شد.")

@requires_permission("add_channel")
async def admin_addchannel_start(update, context):
    context.user_data['state'] = "admin_addchannel_username"
    context.user_data['data'] = {}
    await update.message.reply_text(
        "یوزرنیم کانال رو بدون @ ارسال کنید (مثلا mychannel).\n"
        "⚠️ ربات باید ادمین آن کانال باشد تا بتواند عضویت را چک کند.",
        reply_markup=cancel_kb()
    )

@requires_permission("del_channel")
async def admin_delchannel_start(update, context):
    channels = get_required_channels()
    if not channels:
        await update.message.reply_text("هیچ کانال اجباری‌ای ثبت نشده است.")
        return
    kb = [[IKB(f"{c['channel_title']} 🗑", callback_data=f"delchannel_{c['id']}")] for c in channels]
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدام کانال حذف شود؟", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def admin_delchannel_do(update, context, channel_id):
    query = update.callback_query
    conn = get_conn()
    conn.execute("DELETE FROM required_channels WHERE id=?", (channel_id,))
    conn.commit()
    conn.close()
    await query.edit_message_text("✅ کانال حذف شد.")

async def admin_callback_router(update, context, data):
    if data.startswith("editplan_sel_"):
        await admin_edit_plan_menu(update, context, int(data.split("_")[2]))
    elif data.startswith("editfield_"):
        parts = data.split("_")
        field, plan_id = parts[1], int(parts[2])
        await admin_edit_field_start(update, context, field, plan_id)
    elif data.startswith("delcfg_"):
        parts = data.split("_")
        await admin_delete_single_config(update, context, int(parts[1]), int(parts[2]))
    elif data.startswith("viewcfg_"):
        await admin_view_single_config(update, context, int(data.split("_")[1]))
    elif data.startswith("delplan_confirm_"):
        await admin_del_plan_confirm(update, context, int(data.split("_")[2]))
    elif data.startswith("revokecode_"):
        await admin_revoke_discount_do(update, context, data.split("_", 1)[1])
    elif data == "discscope_all":
        await admin_discount_scope_all(update, context)
    elif data.startswith("discscope_cat_"):
        await admin_discount_scope_category(update, context, data[len("discscope_cat_"):])
    elif data == "discscope_plan":
        await admin_discount_scope_plan_list(update, context)
    elif data.startswith("discscopeplan_"):
        await admin_discount_scope_plan_pick(update, context, int(data.split("_")[1]))
    elif data == "discscope_back":
        kb = InlineKeyboardMarkup([
            [IKB("🌐 همه‌ی پلن‌ها", callback_data="discscope_all")],
            [IKB(f"{CATEGORY_LABELS['حجمی']} (فقط این دسته)", callback_data="discscope_cat_حجمی")],
            [IKB(f"{CATEGORY_LABELS['نامحدود']} (فقط این دسته)", callback_data="discscope_cat_نامحدود")],
            [IKB("🎯 فقط یک پلن خاص", callback_data="discscope_plan")],
        ])
        await update.callback_query.edit_message_text("این کد تخفیف برای کدوم پلن‌ها معتبر باشه؟", reply_markup=kb)
    elif data.startswith("viewuserorders_"):
        await admin_view_user_orders(update, context, int(data.split("_")[1]))
    elif data.startswith("adminvieworder_"):
        await admin_view_order_config(update, context, int(data.split("_")[1]))
    elif data.startswith("unban_"):
        await admin_unban_do(update, context, int(data.split("_")[1]))
    elif data.startswith("removeadmin_"):
        await admin_del_admin_do(update, context, int(data.split("_")[1]))
    elif data.startswith("delchannel_"):
        await admin_delchannel_do(update, context, int(data.split("_")[1]))
    elif data.startswith("newplancat_"):
        await admin_new_plan_finish(update, context, data[len("newplancat_"):])
    elif data.startswith("editcat_"):
        await admin_edit_plan_list_by_category(update, context, data[len("editcat_"):])
    elif data.startswith("delcat_"):
        await admin_del_plan_list_by_category(update, context, data[len("delcat_"):])
    elif data.startswith("setcat_"):
        rest = data[len("setcat_"):]
        plan_id_str, category = rest.split("_", 1)
        await admin_set_plan_category(update, context, int(plan_id_str), category)
    elif data.startswith("deltest_"):
        await admin_delete_test_do(update, context, int(data.split("_")[1]))
    elif data.startswith("edittest_sel_"):
        await admin_edit_test_select(update, context, int(data.split("_")[2]))
    elif data.startswith("selnewadmin_"):
        target_id = int(data.split("_")[1])
        if is_admin(target_id):
            await qanswer(update.callback_query, "این کاربر از قبل ادمین است.", show_alert=True)
            return
        await start_permission_toggle(update, context, target_id, is_new=True)
    elif data.startswith("editadminperm_"):
        target_id = int(data.split("_")[1])
        await start_permission_toggle(update, context, target_id, is_new=False)
    elif data.startswith("transferownok_"):
        await admin_transfer_ownership_do(update, context, int(data.split("_")[1]))
    elif data.startswith("transferown_"):
        await admin_transfer_ownership_confirm(update, context, int(data.split("_")[1]))
    elif data == "cfgtype_text":
        context.user_data['state'] = "admin_addcfg_text"
        await update.callback_query.edit_message_text("متن کانفیگ رو ارسال کنید:", reply_markup=cancel_kb())
    elif data == "cfgtype_photo":
        context.user_data['state'] = "admin_addcfg_photo"
        await update.callback_query.edit_message_text("عکس + کپشن (متن دلخواه) رو ارسال کنید:", reply_markup=cancel_kb())
    elif data == "bulkcfg_done":
        await cb_bulkcfg_done(update, context)
    elif data == "wheel_addprize":
        await start_wheel_prize_wizard(update, context)
    elif data.startswith("wheeltype_"):
        await cb_wheel_choose_type(update, context, data[len("wheeltype_"):])
    elif data == "wheel_wizard_done":
        await cb_wheel_wizard_done(update, context)
    elif data == "wheel_wizard_continue":
        clear_state(context)
        await update.callback_query.edit_message_text("✅ رد شد.")
        await start_wheel_prize_wizard(update, context)
    elif data == "wheel_listprizes":
        await cb_wheel_list_prizes(update, context)
    elif data.startswith("wheeldelprize_"):
        await cb_wheel_delete_prize(update, context, int(data.split("_")[1]))
    elif data.startswith("wheeladdcfg_"):
        await cb_wheel_addcfg_start(update, context, int(data.split("_")[1]))
    elif data == "wheelcfgtype_text":
        context.user_data['state'] = "wheel_addcfg_text"
        await update.callback_query.edit_message_text("متن کانفیگ رو ارسال کنید:", reply_markup=cancel_kb())
    elif data == "wheelcfgtype_photo":
        context.user_data['state'] = "wheel_addcfg_photo"
        await update.callback_query.edit_message_text("عکس + کپشن رو ارسال کنید:", reply_markup=cancel_kb())
    elif data == "wheel_setcooldown":
        await cb_wheel_setcooldown_start(update, context)
    elif data == "wheel_spin":
        await cb_wheel_spin(update, context)
    elif data == "testtype_text":
        context.user_data['state'] = "admin_addtest_text"
        await update.callback_query.edit_message_text("متن کانفیگ تست رو ارسال کنید:", reply_markup=cancel_kb())
    elif data == "testtype_photo":
        context.user_data['state'] = "admin_addtest_photo"
        await update.callback_query.edit_message_text("عکس + کپشن کانفیگ تست رو ارسال کنید:", reply_markup=cancel_kb())

async def handle_admin_text_states(update, context, state, text) -> bool:
    if not is_admin(update.effective_user.id):
        return False
    d = context.user_data.get('data', {})

    if await extra_text_states(update, context, state, text):
        return True

    if state == "admin_new_plan_title":
        d['title'] = text
        context.user_data['state'] = "admin_new_plan_price"
        await update.message.reply_text("قیمت پلن رو به تومان ارسال کنید (فقط عدد):")
        return True

    if state == "admin_new_plan_price":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        d['price'] = int(text)
        context.user_data['state'] = "admin_new_plan_duration"
        await update.message.reply_text("مدت اعتبار این پلن رو به روز ارسال کنید (فقط عدد، مثلا 30):")
        return True

    if state == "admin_new_plan_duration":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        d['duration'] = int(text)
        context.user_data['state'] = "admin_new_plan_category"
        kb = InlineKeyboardMarkup([[IKB(CATEGORY_LABELS[c], callback_data=f"newplancat_{c}")] for c in PLAN_CATEGORIES])
        await update.message.reply_text("این پلن مربوط به کدوم دسته است؟ 👇", reply_markup=kb)
        return True

    if state == "admin_edit_title":
        conn = get_conn()
        conn.execute("UPDATE plans SET title=? WHERE id=?", (text, d['edit_plan_id']))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ عنوان بروزرسانی شد.")
        return True

    if state == "admin_edit_price":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        conn = get_conn()
        conn.execute("UPDATE plans SET price=? WHERE id=?", (int(text), d['edit_plan_id']))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ قیمت بروزرسانی شد.")
        return True

    if state == "admin_edit_duration":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        conn = get_conn()
        conn.execute("UPDATE plans SET duration_days=? WHERE id=?", (int(text), d['edit_plan_id']))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ مدت اعتبار بروزرسانی شد.")
        return True

    if state == "wheel_discount_percent":
        if not text.isdigit() or not (1 <= int(text) <= 100):
            await update.message.reply_text("لطفا عددی بین ۱ تا ۱۰۰ ارسال کنید:")
            return True
        d['discount_percent'] = int(text)
        context.user_data['state'] = "wheel_discount_hours"
        await update.message.reply_text("این کد تخفیف چند ساعت اعتبار داشته باشه؟ (فقط عدد، مثلا 3):")
        return True

    if state == "wheel_discount_hours":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("لطفا فقط عدد معتبر ارسال کنید:")
            return True
        d['discount_hours'] = int(text)
        context.user_data['state'] = "wheel_weight"
        await update.message.reply_text("شانس (احتمال) این جایزه چند درصد باشه؟ (فقط عدد، مثلا 30):")
        return True

    if state == "wheel_config_gb":
        try:
            gb = float(text.strip().replace(",", "."))
            if gb <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("لطفا فقط عدد معتبر بزرگتر از صفر ارسال کنید:")
            return True
        d['config_gb'] = gb
        context.user_data['state'] = "wheel_weight"
        await update.message.reply_text("شانس (احتمال) این جایزه چند درصد باشه؟ (فقط عدد، مثلا 30):")
        return True

    if state == "wheel_weight":
        if not text.isdigit() or not (1 <= int(text) <= 100):
            await update.message.reply_text("لطفا عددی بین ۱ تا ۱۰۰ ارسال کنید:")
            return True
        weight = int(text)
        ptype = d.get('prize_type')
        prize_id = add_wheel_prize(
            ptype, weight,
            discount_percent=d.get('discount_percent'),
            discount_hours=d.get('discount_hours'),
            config_gb=d.get('config_gb'),
        )
        clear_state(context)
        await update.message.reply_text(f"✅ جایزه «{WHEEL_PRIZE_LABELS.get(ptype, ptype)}» با شانس {weight}% اضافه شد.")
        if ptype == "config":
            await update.message.reply_text("حالا کانفیگ‌های واقعی این جایزه رو وارد کن (یا از منوی مدیریت جایزه‌ها بعداً اضافه‌شون کن):")
            context.user_data['data'] = {"wheel_prize_id": prize_id}
            kb = InlineKeyboardMarkup([
                [IKB("متنی 📝", callback_data="wheelcfgtype_text"),
                 IKB("عکس + کپشن 🖼", callback_data="wheelcfgtype_photo")],
                [IKB("رد شدن ⏭", callback_data="wheel_wizard_continue")],
            ])
            await update.message.reply_text("نوع کانفیگ رو انتخاب کنید:", reply_markup=kb)
        else:
            await start_wheel_prize_wizard(update, context)
        return True

    if state == "wheel_cooldown_hours":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("لطفا فقط عدد معتبر ارسال کنید:")
            return True
        set_wheel_cooldown(int(text))
        clear_state(context)
        await update.message.reply_text(f"✅ فاصله زمانی هر کاربر روی {text} ساعت تنظیم شد.")
        return True

    if state == "wheel_addcfg_text":
        conn = get_conn()
        conn.execute("INSERT INTO wheel_prize_configs(prize_id, content_type, text_content) VALUES (?,?,?)",
                     (d['wheel_prize_id'], 'text', text))
        conn.commit()
        conn.close()
        await update.message.reply_text("✅ اضافه شد.")
        kb = InlineKeyboardMarkup([
            [IKB("متنی 📝", callback_data="wheelcfgtype_text"),
             IKB("عکس + کپشن 🖼", callback_data="wheelcfgtype_photo")],
            [IKB("پایان ✅", callback_data="cancel_action")],
        ])
        await update.message.reply_text("کانفیگ بعدی یا پایان:", reply_markup=kb)
        return True

    if state == "admin_card_number":
        d['new_card_number'] = text.strip()
        context.user_data['state'] = "admin_card_holder"
        await update.message.reply_text("این شماره کارت به نام چه کسی است؟ (نام و نام خانوادگی رو ارسال کنید):", reply_markup=cancel_kb())
        return True

    if state == "admin_card_holder":
        holder = text.strip()
        set_card_info(d['new_card_number'], holder)
        clear_state(context)
        await update.message.reply_text(f"✅ شماره کارت بروزرسانی شد:\n💳 {d['new_card_number']}\n👤 به نام: {holder}")
        if is_owner(update.effective_user.id):
            await update.message.reply_text("🛠 پنل مدیریت — یکی از گزینه‌ها رو انتخاب کنید:", reply_markup=admin_menu_kb(update.effective_user.id))
        return True

    if state == "admin_welcome_message":
        clear_state(context)
        if text.strip() in ("پیش‌فرض", "پیش فرض", "default"):
            set_custom_welcome_message(None)
            await update.message.reply_text(f"✅ پیام خوش‌آمدگویی به حالت پیش‌فرض بازگشت:\n\n{build_welcome_message(context.bot.first_name)}")
        else:
            set_custom_welcome_message(text)
            bot_name = context.bot.first_name
            await update.message.reply_text(f"✅ پیام خوش‌آمدگویی بروزرسانی شد:\n\n{text.replace('{نام}', bot_name).replace('{bot_name}', bot_name)}")
        if is_owner(update.effective_user.id) or has_permission(update.effective_user.id, "welcome"):
            await update.message.reply_text("🛠 پنل مدیریت — یکی از گزینه‌ها رو انتخاب کنید:", reply_markup=admin_menu_kb(update.effective_user.id))
        return True

    if state == "admin_maintenance_off_text":
        maintenance_text = text
        set_maintenance(True)
        clear_state(context)
        await update.message.reply_text("⏳ در حال ارسال پیام به همه کاربران...")
        await broadcast_maintenance(context, maintenance_text)
        await update.message.reply_text("🔴 ربات خاموش شد. کاربران عادی تا روشن‌شدن دوباره نمی‌توانند از ربات استفاده کنند.")
        return True

    if state == "admin_addcfg_text":
        conn = get_conn()
        conn.execute("INSERT INTO plan_configs(plan_id, content_type, text_content) VALUES (?,?,?)",
                     (d['edit_plan_id'], 'text', text))
        conn.commit()
        conn.close()
        if d.get('bulk_add'):
            d['bulk_count'] = d.get('bulk_count', 0) + 1
            await update.message.reply_text("✅ اضافه شد.")
            await prompt_next_bulk_config(update, context)
        else:
            clear_state(context)
            await update.message.reply_text("✅ کانفیگ متنی به پلن اضافه شد. (ظرفیت +۱)")
        return True

    if state == "admin_addtest_text":
        conn = get_conn()
        conn.execute("INSERT INTO test_configs(content_type, text_content) VALUES ('text', ?)", (text,))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ کانفیگ تست متنی اضافه شد.")
        return True

    if state == "admin_edittest_text":
        conn = get_conn()
        conn.execute("UPDATE test_configs SET text_content=? WHERE id=?", (text, d['edit_test_id']))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ کانفیگ تست متنی ویرایش شد.")
        return True

    if state == "admin_discount_code":
        d['code'] = text.strip()
        context.user_data['state'] = "admin_discount_amount"
        await update.message.reply_text("چقدر از قیمت کم شود؟ (به تومان، فقط عدد):")
        return True

    if state == "admin_discount_amount":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        d['amount'] = int(text)
        context.user_data['state'] = "admin_discount_days"
        await update.message.reply_text("چند روز اعتبار داشته باشد؟ (فقط عدد، برای نامحدود عدد 0 را بفرستید):")
        return True

    if state == "admin_discount_days":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        days = int(text)
        d['expires_at'] = "" if days == 0 else (datetime.now() + timedelta(days=days)).isoformat()
        context.user_data['state'] = "admin_discount_uses"
        await update.message.reply_text("برای چند نفر کار کند؟ (عدد بفرستید، یا کلمه نامحدود را ارسال کنید):")
        return True

    if state == "admin_discount_uses":
        if text.strip() == "نامحدود":
            max_uses = -1
        elif text.isdigit():
            max_uses = int(text)
        else:
            await update.message.reply_text("لطفا فقط عدد یا کلمه «نامحدود» ارسال کنید:")
            return True
        d['max_uses'] = max_uses
        context.user_data['state'] = "admin_discount_scope"
        kb = InlineKeyboardMarkup([
            [IKB("🌐 همه‌ی پلن‌ها", callback_data="discscope_all")],
            [IKB(f"{CATEGORY_LABELS['حجمی']} (فقط این دسته)", callback_data="discscope_cat_حجمی")],
            [IKB(f"{CATEGORY_LABELS['نامحدود']} (فقط این دسته)", callback_data="discscope_cat_نامحدود")],
            [IKB("🎯 فقط یک پلن خاص", callback_data="discscope_plan")],
        ])
        await update.message.reply_text("این کد تخفیف برای کدوم پلن‌ها معتبر باشه؟", reply_markup=kb)
        return True

    if state == "admin_direct_userid":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط آیدی عددی معتبر ارسال کنید:")
            return True
        d['target_id'] = int(text)
        context.user_data['state'] = "admin_direct_message"
        await update.message.reply_text("متن پیام رو ارسال کنید:")
        return True

    if state == "admin_direct_message":
        try:
            await copy_message_to_user(context, d['target_id'], update.effective_chat.id, update.message.message_id)
            await update.message.reply_text("✅ پیام ارسال شد.")
        except TelegramError as e:
            await update.message.reply_text(f"خطا در ارسال پیام: {e}")
        clear_state(context)
        return True

    if state == "admin_broadcast_text":
        conn = get_conn()
        users = conn.execute("SELECT user_id FROM users WHERE banned=0").fetchall()
        conn.close()
        sent, failed = 0, 0
        for u in users:
            try:
                await context.bot.copy_message(chat_id=u['user_id'], from_chat_id=update.effective_chat.id, message_id=update.message.message_id)
                sent += 1
            except TelegramError:
                failed += 1
        await update.message.reply_text(f"✅ پیام همگانی ارسال شد.\nموفق: {sent} | ناموفق: {failed}")
        clear_state(context)
        return True

    if state == "admin_chargewallet_userid":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط آیدی عددی معتبر ارسال کنید:")
            return True
        d['target_id'] = int(text)
        context.user_data['state'] = "admin_chargewallet_amount"
        await update.message.reply_text(f"موجودی فعلی این کاربر: {get_wallet(d['target_id']):,} تومان\nمبلغ شارژ رو به تومان ارسال کنید (فقط عدد):")
        return True

    if state == "admin_chargewallet_amount":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        amount = int(text)
        change_wallet(d['target_id'], amount)
        try:
            await context.bot.send_message(d['target_id'], f"کیف پول شما از طرف مالک به مبلغ {amount:,} تومان شارژ شد.")
        except TelegramError:
            pass
        await update.message.reply_text(f"✅ کیف پول کاربر {d['target_id']} به مبلغ {amount:,} تومان شارژ شد.")
        clear_state(context)
        return True

    if state == "admin_decwallet_userid":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط آیدی عددی معتبر ارسال کنید:")
            return True
        d['target_id'] = int(text)
        context.user_data['state'] = "admin_decwallet_amount"
        await update.message.reply_text(f"موجودی فعلی این کاربر: {get_wallet(d['target_id']):,} تومان\nچقدر کم شود؟ (فقط عدد):")
        return True

    if state == "admin_decwallet_amount":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط عدد ارسال کنید:")
            return True
        amount = int(text)
        change_wallet(d['target_id'], -amount)

        await update.message.reply_text(f"✅ مبلغ {amount:,} تومان از کیف پول کاربر {d['target_id']} کسر شد.\nموجودی جدید: {get_wallet(d['target_id']):,} تومان")
        clear_state(context)
        return True

    if state == "admin_ban_userid":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط آیدی عددی معتبر ارسال کنید:")
            return True
        target_id = int(text)
        if is_admin(target_id):
            clear_state(context)
            await update.message.reply_text("⛔️ امکان مسدود کردن ادمین‌ها و مالک ربات وجود ندارد.")
            return True
        d['target_id'] = target_id
        context.user_data['state'] = "admin_ban_reason"
        await update.message.reply_text("دلیل مسدود کردن این کاربر رو ارسال کنید:")
        return True

    if state == "admin_ban_reason":
        target_id = d['target_id']
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO users(user_id, wallet, banned) VALUES (?,0,1)", (target_id,))
        conn.execute("UPDATE users SET banned=1, ban_reason=? WHERE user_id=?", (text, target_id))
        conn.commit()
        conn.close()
        try:
            await context.bot.send_message(target_id, f"کاربر محترم شما از طرف ادمین مسدود شده اید ❌\nدلیل: {text}")
        except TelegramError:
            pass
        await update.message.reply_text(f"✅ کاربر {target_id} مسدود شد.\nدلیل ثبت‌شده: {text}")
        clear_state(context)
        return True

    if state == "admin_add_admin_id":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط آیدی عددی معتبر ارسال کنید:")
            return True
        new_admin_id = int(text)
        if is_admin(new_admin_id):
            clear_state(context)
            await update.message.reply_text("این کاربر از قبل ادمین است.")
            return True
        await start_permission_toggle(update, context, new_admin_id, is_new=True)
        return True

    if state == "admin_addchannel_username":
        username = text.strip().lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{username}")
            title = chat.title or username
        except TelegramError:
            title = username
        conn = get_conn()
        conn.execute("INSERT INTO required_channels(channel_username, channel_title) VALUES (?,?)", (username, title))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text(f"✅ کانال «{title}» به لیست عضویت اجباری اضافه شد.\n⚠️ کاربرانی که قبلا عضو ربات بودن، از الان مجبورن این کانال جدید رو هم عضو بشن.")
        return True

    if state == "admin_reply_ticket_text":
        await admin_finish_ticket_reply(update, context)
        return True

    return False

async def admin_finish_ticket_reply(update, context):
    """پاسخ ادمین به تیکت رو (متن/عکس/فیلم/ویس، با حفظ کامل فرمت‌بندی) برای کاربر ارسال می‌کنه."""
    d = context.user_data.get('data', {})
    ticket_id = d.get('ticket_id')
    conn = get_conn()
    ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    conn.execute("UPDATE tickets SET status='پاسخ داده شد' WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()
    ok = False
    if ticket:
        try:
            await context.bot.send_message(ticket['user_id'], "📩 پاسخ پشتیبانی:")
        except TelegramError:
            pass
        ok = await copy_message_to_user(context, ticket['user_id'], update.effective_chat.id, update.message.message_id)
    if ok:
        await update.message.reply_text("✅ پاسخ برای کاربر ارسال شد.")
    else:
        await update.message.reply_text("⚠️ ارسال پاسخ به کاربر ناموفق بود (احتمالاً کاربر ربات رو بلاک کرده یا هیچوقت استارت نزده).")
    clear_state(context)

async def handle_admin_photo_states(update, context, state, photo_file_id):
    if not is_admin(update.effective_user.id):
        return
    d = context.user_data.get('data', {})
    caption = update.message.caption or ""

    if state == "admin_addcfg_photo":
        conn = get_conn()
        conn.execute("INSERT INTO plan_configs(plan_id, content_type, photo_file_id, caption) VALUES (?,?,?,?)",
                     (d['edit_plan_id'], 'photo', photo_file_id, caption))
        conn.commit()
        conn.close()
        if d.get('bulk_add'):
            d['bulk_count'] = d.get('bulk_count', 0) + 1
            await update.message.reply_text("✅ اضافه شد.")
            await prompt_next_bulk_config(update, context)
        else:
            clear_state(context)
            await update.message.reply_text("✅ کانفیگ عکسی به پلن اضافه شد. (ظرفیت +۱)")
        return

    if state == "wheel_addcfg_photo":
        conn = get_conn()
        conn.execute("INSERT INTO wheel_prize_configs(prize_id, content_type, photo_file_id, caption) VALUES (?,?,?,?)",
                     (d['wheel_prize_id'], 'photo', photo_file_id, caption))
        conn.commit()
        conn.close()
        await update.message.reply_text("✅ اضافه شد.")
        kb = InlineKeyboardMarkup([
            [IKB("متنی 📝", callback_data="wheelcfgtype_text"),
             IKB("عکس + کپشن 🖼", callback_data="wheelcfgtype_photo")],
            [IKB("پایان ✅", callback_data="cancel_action")],
        ])
        await update.message.reply_text("کانفیگ بعدی یا پایان:", reply_markup=kb)
        return

    if state == "admin_addtest_photo":
        conn = get_conn()
        conn.execute("INSERT INTO test_configs(content_type, photo_file_id, caption) VALUES ('photo', ?, ?)",
                     (photo_file_id, caption))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ کانفیگ تست عکسی اضافه شد.")
        return

    if state == "admin_edittest_photo":
        conn = get_conn()
        conn.execute("UPDATE test_configs SET photo_file_id=?, caption=? WHERE id=?",
                     (photo_file_id, caption, d['edit_test_id']))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ کانفیگ تست عکسی ویرایش شد.")
        return

async def _post_init(app):
    """اگه دیتابیس روی مسیر اصلی (Volume) باز نشده باشه، به مالک‌ها هشدار می‌ده."""
    if not DB_WARNING:
        return
    for oid in get_admin_ids_with("__owners_only__"):
        try:
            await app.bot.send_message(
                oid,
                "⚠️ هشدار: دیتابیس روی Volume ساخته نشد و از مسیر موقت استفاده می‌شه؛ با هر ری‌دیپلوی اطلاعات پاک می‌شه.\n"
                "در Railway متغیر RAILWAY_RUN_UID=0 رو اضافه کنید و مطمئن بشید Volume روی /data مونت شده.\n\n"
                f"جزئیات: {DB_WARNING}")
        except Exception:
            pass

# ═════════════════════════ بخش کوین: تنظیمات، زیرمجموعه‌گیری، خرید با کوین، کوین روزانه ═════════════════════════

def get_setting(key, default=None):
    try:
        conn = get_conn()
        row = conn.execute("SELECT value FROM feature_settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    except Exception:
        return default

def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT INTO feature_settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()
    conn.close()

def feature_on(name):
    """name یکی از: referral, daily, coinshop (پیش‌فرض همه خاموش)."""
    return get_setting(f"{name}_enabled", "0") == "1"

def set_feature(name, flag):
    set_setting(f"{name}_enabled", "1" if flag else "0")

def coin_features_on():
    return feature_on("referral") or feature_on("daily") or feature_on("coinshop")

def get_int_setting(key, default):
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default

def get_coins(user_id):
    conn = get_conn()
    r = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return (r['coins'] or 0) if r else 0

def change_coins(user_id, delta):
    conn = get_conn()
    conn.execute("UPDATE users SET coins = MAX(0, COALESCE(coins,0) + ?) WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def normalize_link(text):
    """لینک ورودی ادمین رو به URL معتبر تبدیل می‌کنه؛ اگه معتبر نبود None برمی‌گردونه."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("@"):
        t = t[1:]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", t):
        return f"https://t.me/{t}"
    if t.startswith(("t.me/", "telegram.me/")):
        return "https://" + t
    if t.startswith(("http://", "https://", "tg://")):
        return t
    return None

async def ui(update, text, markup=None):
    """اگه از دکمه‌ی شیشه‌ای اومده ادیت می‌کنه، وگرنه پیام جدید می‌فرسته."""
    if update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)

# ───────────────────────── زیرمجموعه‌گیری ─────────────────────────
def register_referral(referred_id, referrer_id):
    if referred_id == referrer_id:
        return
    conn = get_conn()
    ref = conn.execute("SELECT banned FROM users WHERE user_id=?", (referrer_id,)).fetchone()
    if ref and not ref['banned']:
        conn.execute("INSERT OR IGNORE INTO referrals(referred_id, referrer_id, created_at, rewarded) VALUES (?,?,?,0)",
                     (referred_id, referrer_id, datetime.now().isoformat()))
        conn.commit()
    conn.close()

def referral_count(user_id):
    conn = get_conn()
    r = conn.execute("SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND rewarded=1", (user_id,)).fetchone()
    conn.close()
    return r['c']

async def reward_referral_if_pending(context, user_id):
    """بعد از اینکه کاربر جدید وارد منوی اصلی شد (و کانال‌های اجباری رو عضو شد)، کوین معرف داده می‌شه."""
    try:
        if not feature_on("referral"):
            return
        conn = get_conn()
        row = conn.execute("SELECT * FROM referrals WHERE referred_id=? AND rewarded=0", (user_id,)).fetchone()
        if not row:
            conn.close()
            return
        reward = get_int_setting("referral_reward", 5)
        cur = conn.execute("UPDATE referrals SET rewarded=1 WHERE referred_id=? AND rewarded=0", (user_id,))
        if cur.rowcount == 1:
            conn.execute("UPDATE users SET coins = COALESCE(coins,0) + ? WHERE user_id=?", (reward, row['referrer_id']))
        conn.commit()
        conn.close()
        if cur.rowcount == 1:
            try:
                await context.bot.send_message(
                    row['referrer_id'],
                    f"🎉 یک نفر با لینک دعوت شما وارد ربات شد!\n🪙 {reward:,} کوین به حساب شما اضافه شد.\n"
                    f"موجودی فعلی: {get_coins(row['referrer_id']):,} کوین"
                )
            except TelegramError:
                pass
    except Exception as e:
        logger.warning(f"reward_referral_if_pending error: {e}")

# ───────────────────────── صفحه‌ی اصلی کوین (کاربر) ─────────────────────────
async def show_coin_home(update, context):
    user_id = update.effective_user.id
    kb = []
    lines = [f"🪙 موجودی کوین شما: {get_coins(user_id):,}"]
    if feature_on("referral"):
        kb.append([IKB("👥 زیرمجموعه‌گیری", callback_data="coin_ref")])
        lines.append(f"👥 با دعوت دوستان، به ازای هر نفر {get_int_setting('referral_reward', 5):,} کوین بگیرید.")
    if feature_on("daily"):
        kb.append([IKB("🎁 دریافت کوین روزانه", callback_data="coin_daily")])
        lines.append(f"🎁 هر روز {get_int_setting('daily_reward', 3):,} کوین هدیه بگیرید.")
    if feature_on("coinshop"):
        kb.append([IKB("🛍 خرید با کوین", callback_data="coin_shop")])
        lines.append("🛍 کوین‌هاتون رو با کانفیگ عوض کنید.")
    if not kb:
        lines = ["🪙 بخش کوین در حال حاضر غیرفعال است."]
    kb.append([IKB("بستن ❌", callback_data="cancel_action")])
    await ui(update, "\n".join(lines), InlineKeyboardMarkup(kb))

async def cb_coin_ref(update, context):
    query = update.callback_query
    if not feature_on("referral"):
        await qanswer(query, "این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    uid = update.effective_user.id
    uname = context.bot.username
    if not uname:
        uname = (await context.bot.get_me()).username
    link = f"https://t.me/{uname}?start=ref_{uid}"
    reward = get_int_setting("referral_reward", 5)
    text = (
        "👥 زیرمجموعه‌گیری\n\n"
        f"لینک اختصاصی شما (بزنید کپی بشه):\n{link}\n\n"
        f"🎯 به ازای هر نفری که با لینک شما وارد ربات بشه، {reward:,} کوین می‌گیرید.\n"
        f"👤 تعداد دعوت‌های موفق: {referral_count(uid):,}\n"
        f"🪙 موجودی کوین شما: {get_coins(uid):,}"
    )
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('بیا از این ربات استفاده کن 👇')}"
    kb = InlineKeyboardMarkup([
        [IKB("📤 اشتراک‌گذاری لینک دعوت", url=share)],
        [IKB("↩️ برگشت", callback_data="coin_home")],
    ])
    await safe_edit(query, text, kb)

# ───────────────────────── خرید با کوین ─────────────────────────
def coin_plans():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM plans WHERE COALESCE(is_coin,0)=1 ORDER BY id").fetchall()
    conn.close()
    return rows

async def cb_coin_shop(update, context):
    query = update.callback_query
    if not feature_on("coinshop"):
        await qanswer(query, "این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    plans = coin_plans()
    uid = update.effective_user.id
    if not plans:
        await safe_edit(query, "در حال حاضر کانفیگی برای خرید با کوین موجود نیست.",
                        InlineKeyboardMarkup([[IKB("↩️ برگشت", callback_data="coin_home")]]))
        return
    kb = []
    for p in plans:
        cap = plan_capacity(p['id'])
        kb.append([IKB(f"{p['title']} | {p['coin_price']:,} کوین | موجودی: {cap}",
                       callback_data=f"coinplan_{p['id']}", ckey=f"plan:{p['id']}")])
    kb.append([IKB("↩️ برگشت", callback_data="coin_home")])
    await safe_edit(query, f"🛍 خرید با کوین\n🪙 موجودی شما: {get_coins(uid):,} کوین\n\nیکی از کانفیگ‌ها رو انتخاب کنید:",
                    InlineKeyboardMarkup(kb))

async def cb_coin_plan_confirm(update, context, plan_id):
    query = update.callback_query
    uid = update.effective_user.id
    conn = get_conn()
    p = conn.execute("SELECT * FROM plans WHERE id=? AND COALESCE(is_coin,0)=1", (plan_id,)).fetchone()
    conn.close()
    if not p:
        await safe_edit(query, "این کانفیگ دیگر موجود نیست ❌")
        return
    cap = plan_capacity(plan_id)
    if cap <= 0:
        await qanswer(query, "ظرفیت این کانفیگ تمام شده است ❌", show_alert=True)
        return
    coins = get_coins(uid)
    text = (f"🛍 {p['title']}\n🪙 قیمت: {p['coin_price']:,} کوین\n📅 مدت اعتبار: {p['duration_days']} روز\n"
            f"📦 موجودی: {cap}\n\n🪙 موجودی شما: {coins:,} کوین")
    kb = []
    if coins >= p['coin_price']:
        kb.append([IKB("تایید خرید ✅", callback_data=f"coinbuyok_{plan_id}")])
    else:
        text += f"\n\n❌ کوین کافی نیست (کمبود: {p['coin_price'] - coins:,} کوین)"
    kb.append([IKB("↩️ برگشت", callback_data="coin_shop")])
    await safe_edit(query, text, InlineKeyboardMarkup(kb))

def purchase_coin_plan(user_id, plan_id):
    """خرید اتمیک با کوین؛ (وضعیت، داده) برمی‌گردونه."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute("SELECT * FROM plans WHERE id=? AND COALESCE(is_coin,0)=1", (plan_id,)).fetchone()
        if not plan:
            conn.rollback()
            return "noplan", None
        u = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
        coins = (u['coins'] or 0) if u else 0
        price = plan['coin_price'] or 0
        if coins < price:
            conn.rollback()
            return "nocoins", (coins, price)
        cfg = conn.execute("SELECT * FROM plan_configs WHERE plan_id=? AND used=0 LIMIT 1", (plan_id,)).fetchone()
        if not cfg:
            conn.rollback()
            return "nostock", None
        conn.execute("UPDATE plan_configs SET used=1, used_by=? WHERE id=?", (user_id, cfg['id']))
        conn.execute("UPDATE users SET coins = COALESCE(coins,0) - ? WHERE user_id=?", (price, user_id))
        expires = (datetime.now() + timedelta(days=plan['duration_days'] or 30)).isoformat()
        conn.execute(
            """INSERT INTO orders(user_id, plan_id, plan_title, price, pay_method, status, config_id, created_at, expires_at, is_renew)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (user_id, plan_id, plan['title'], price, "کوین", "تحویل شد", cfg['id'], datetime.now().isoformat(), expires))
        conn.commit()
        return "ok", (cfg, plan, price)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

async def cb_coin_buy(update, context, plan_id):
    query = update.callback_query
    user = update.effective_user
    if not feature_on("coinshop"):
        await qanswer(query, "این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    status, data = purchase_coin_plan(user.id, plan_id)
    if status == "noplan":
        await safe_edit(query, "این کانفیگ دیگر موجود نیست ❌")
        return
    if status == "nocoins":
        coins, price = data
        await safe_edit(query, f"🪙 کوین کافی نیست ❌\nموجودی شما: {coins:,} | قیمت: {price:,} کوین",
                        InlineKeyboardMarkup([[IKB("↩️ برگشت", callback_data="coin_shop")]]))
        return
    if status == "nostock":
        await safe_edit(query, "ظرفیت این کانفیگ تمام شده است ❌",
                        InlineKeyboardMarkup([[IKB("↩️ برگشت", callback_data="coin_shop")]]))
        return
    cfg, plan, price = data
    await safe_edit(query, f"✅ خرید با موفقیت انجام شد و کانفیگ ارسال گردید.\n🪙 موجودی جدید: {get_coins(user.id):,} کوین")
    await send_config_to_user(context, user.id, cfg)
    await notify_all_admins_text(
        context,
        f"🪙 خرید جدید با کوین\nکاربر: {user.id} (@{user.username or '-'})\nکانفیگ: {plan['title']}\n"
        f"مبلغ پرداختی: {price:,} کوین", perm="orders")

# ───────────────────────── کوین روزانه + ماموریت‌ها ─────────────────────────
def get_active_missions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM coin_missions WHERE active=1 ORDER BY id").fetchall()
    conn.close()
    return rows

def missions_done_today(user_id):
    conn = get_conn()
    rows = conn.execute("SELECT mission_id FROM mission_done WHERE user_id=? AND day=?", (user_id, today_key())).fetchall()
    conn.close()
    return {r['mission_id'] for r in rows}

def daily_claimed_today(user_id):
    conn = get_conn()
    r = conn.execute("SELECT 1 FROM daily_claims WHERE user_id=? AND day=?", (user_id, today_key())).fetchone()
    conn.close()
    return r is not None

def parse_wait(text):
    """ورودی ادمین رو به ثانیه تبدیل می‌کنه: 30 یا 30s = ثانیه، 2m = دقیقه، 0 = بدون انتظار. نامعتبر → None."""
    t = (text or "").strip().lower().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    m = re.fullmatch(r"(\d+)\s*(s|m)?", t)
    if not m:
        return None
    n = int(m.group(1)) * (60 if m.group(2) == "m" else 1)
    return n if 0 <= n <= 86400 else None

def fmt_wait(sec):
    if not sec:
        return "بدون انتظار"
    if sec % 60 == 0:
        return f"{sec // 60} دقیقه"
    return f"{sec} ثانیه"

def mark_mission_seen(user_id, mission_id):
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO mission_seen(user_id, mission_id, day, seen_at) VALUES (?,?,?,?)",
                 (user_id, mission_id, today_key(), time.time()))
    conn.commit()
    conn.close()

def mission_seen_at(user_id, mission_id):
    conn = get_conn()
    r = conn.execute("SELECT seen_at FROM mission_seen WHERE user_id=? AND mission_id=? AND day=?",
                     (user_id, mission_id, today_key())).fetchone()
    conn.close()
    return r['seen_at'] if r else None

def mission_link(m):
    if m['link']:
        return m['link']
    if m['kind'] == 'member' and m['channel']:
        return f"https://t.me/{m['channel']}"
    return None

async def render_daily(update, context):
    uid = update.effective_user.id
    back = InlineKeyboardMarkup([[IKB("↩️ برگشت", callback_data="coin_home")]])
    if daily_claimed_today(uid):
        await ui(update, f"✅ امروز کوین روزانه‌ات رو گرفتی.\n⏳ {fmt_remaining(seconds_to_next_day())} دیگه دوباره بیا.\n"
                         f"🪙 موجودی شما: {get_coins(uid):,} کوین", back)
        return
    reward = get_int_setting("daily_reward", 3)
    missions = get_active_missions()
    done = missions_done_today(uid)
    lines = [f"🎁 کوین روزانه: {reward:,} کوین"]
    kb = []
    if missions:
        lines.append("\nبرای دریافت کوین، ماموریت‌های زیر رو انجام بده:")
        for i, m in enumerate(missions, 1):
            mark = "✅" if m['id'] in done else "⬜"
            lines.append(f"\n{mark} {i}. {m['title']}")
            if m['description']:
                lines.append(f"    {m['description']}")
            if m['id'] not in done:
                mark_mission_seen(uid, m['id'])
                if m['wait_seconds']:
                    lines.append(f"    ⏱ بعد از {fmt_wait(m['wait_seconds'])} می‌تونی تایید کنی")
                row = []
                link = mission_link(m)
                if link:
                    row.append(IKB(f"🔗 لینک ({i})", url=link, ckey="i:missionlink"))
                row.append(IKB(f"انجام دادم ✅ ({i})", callback_data=f"dailydone_{m['id']}"))
                kb.append(row)
    kb.append([IKB("🎁 دریافت کوین", callback_data="dailyclaim")])
    kb.append([IKB("↩️ برگشت", callback_data="coin_home")])
    await ui(update, "\n".join(lines), InlineKeyboardMarkup(kb))

async def cb_coin_daily(update, context):
    if not feature_on("daily"):
        await qanswer(update.callback_query, "این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    await render_daily(update, context)

async def cb_daily_done(update, context, mission_id):
    query = update.callback_query
    uid = update.effective_user.id
    if not feature_on("daily"):
        await qanswer(query, "این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    conn = get_conn()
    m = conn.execute("SELECT * FROM coin_missions WHERE id=? AND active=1", (mission_id,)).fetchone()
    conn.close()
    if not m:
        await qanswer(query, "این ماموریت دیگر فعال نیست.", show_alert=True)
        await render_daily(update, context)
        return
    wait = m['wait_seconds'] or 0
    if wait:
        seen = mission_seen_at(uid, mission_id)
        if seen is None:
            mark_mission_seen(uid, mission_id)
            seen = time.time()
        remaining = int(seen + wait - time.time())
        if remaining > 0:
            await qanswer(query, "⏳ هنوز ماموریت رو انجام ندادی! اول ماموریت رو انجام بده و بعد دوباره امتحان کن.", show_alert=True)
            return
    if m['kind'] == 'member':
        try:
            member = await context.bot.get_chat_member(f"@{m['channel']}", uid)
            if member.status in ("left", "kicked"):
                await qanswer(query, "❌ هنوز عضو کانال نشدی. اول عضو شو و دوباره بزن.", show_alert=True)
                return
        except TelegramError:
            await qanswer(query, "⚠️ نتونستم عضویتت رو بررسی کنم. کمی بعد دوباره امتحان کن یا به پشتیبانی بگو.", show_alert=True)
            return
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO mission_done(user_id, mission_id, day) VALUES (?,?,?)", (uid, mission_id, today_key()))
    conn.commit()
    conn.close()
    await qanswer(query, "✅ ثبت شد")
    await render_daily(update, context)

async def cb_daily_claim(update, context):
    query = update.callback_query
    uid = update.effective_user.id
    if not feature_on("daily"):
        await qanswer(query, "این بخش در حال حاضر غیرفعال است.", show_alert=True)
        return
    if daily_claimed_today(uid):
        await qanswer(query, "امروز کوین روزانه‌ات رو گرفتی ✅", show_alert=True)
        await render_daily(update, context)
        return
    done = missions_done_today(uid)
    pending = [m for m in get_active_missions() if m['id'] not in done]
    if pending:
        await qanswer(query, f"اول باید همه‌ی ماموریت‌ها رو انجام بدی (باقی‌مانده: {len(pending)}) ⬜", show_alert=True)
        return
    reward = get_int_setting("daily_reward", 3)
    conn = get_conn()
    try:
        conn.execute("INSERT INTO daily_claims(user_id, day) VALUES (?,?)", (uid, today_key()))
    except sqlite3.IntegrityError:
        conn.close()
        await qanswer(query, "امروز کوین روزانه‌ات رو گرفتی ✅", show_alert=True)
        return
    conn.execute("UPDATE users SET coins = COALESCE(coins,0) + ? WHERE user_id=?", (reward, uid))
    conn.commit()
    conn.close()
    await safe_edit(query, f"🎉 {reward:,} کوین به حساب شما اضافه شد!\n🪙 موجودی جدید: {get_coins(uid):,} کوین\n\nفردا دوباره بیا 😉",
                    InlineKeyboardMarkup([[IKB("↩️ برگشت", callback_data="coin_home")]]))


def _order_price_text(o):
    """قیمت سفارش: برای خرید با کوین «کوین» و برای بقیه «تومان»."""
    if o['pay_method'] == "کوین":
        return f"{o['price']:,} کوین"
    return f"{o['price']:,} تومان"


# ───────────────────────── ادمین: کانفیگ‌های کوین ─────────────────────────
@requires_permission("add_coin_plan")
async def admin_add_coin_plan_start(update, context):
    context.user_data['state'] = "admin_new_cplan_title"
    context.user_data['data'] = {}
    await update.message.reply_text("🪙 عنوان (تایتل) کانفیگ کوین جدید رو ارسال کنید:", reply_markup=cancel_kb())

@requires_permission("list_coin_plan")
async def admin_list_coin_plans(update, context):
    plans = coin_plans()
    if not plans:
        await update.message.reply_text("هیچ کانفیگ کوینی ثبت نشده است.")
        return
    lines = [f"🪙 کانفیگ‌های کوین ({len(plans)} مورد):\n"]
    for p in plans:
        lines.append(f"#{p['id']} — {p['title']} — {p['coin_price']:,} کوین — {p['duration_days']} روز — موجودی: {plan_capacity(p['id'])}")
    await update.message.reply_text("\n".join(lines))

@requires_permission("edit_coin_plan")
async def admin_edit_coin_plan_start(update, context):
    plans = coin_plans()
    if not plans:
        await update.message.reply_text("هیچ کانفیگ کوینی برای ویرایش ثبت نشده است.")
        return
    kb = [[IKB(f"{p['title']} ({p['coin_price']:,} کوین)", callback_data=f"ecp_{p['id']}", ckey=f"plan:{p['id']}")] for p in plans]
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدام کانفیگ کوین ویرایش شود؟", reply_markup=InlineKeyboardMarkup(kb))

async def admin_edit_coin_plan_menu(update, context, plan_id):
    query = update.callback_query
    conn = get_conn()
    p = conn.execute("SELECT * FROM plans WHERE id=? AND COALESCE(is_coin,0)=1", (plan_id,)).fetchone()
    conn.close()
    if not p:
        await safe_edit(query, "این کانفیگ کوین دیگر وجود ندارد.")
        return
    context.user_data['data'] = {"edit_plan_id": plan_id}
    text = (f"🪙 {p['title']}\nقیمت: {p['coin_price']:,} کوین\nمدت اعتبار: {p['duration_days']} روز\n"
            f"موجودی: {plan_capacity(plan_id)}\n\nچه چیزی را می‌خواهید ویرایش کنید؟")
    kb = InlineKeyboardMarkup([
        [IKB("تغییر عنوان ✏️", callback_data=f"ecpf_title_{plan_id}")],
        [IKB("تغییر قیمت (کوین) 🪙", callback_data=f"ecpf_price_{plan_id}")],
        [IKB("تغییر مدت اعتبار 📅", callback_data=f"ecpf_duration_{plan_id}")],
        [IKB("افزودن کانفیگ به این پلن ➕", callback_data=f"editfield_addcfg_{plan_id}")],
        [IKB("مشاهده/حذف کانفیگ‌های این پلن 📋", callback_data=f"editfield_listcfg_{plan_id}")],
        [IKB("لغو ❌", callback_data="cancel_action")],
    ])
    await safe_edit(query, text, kb)

async def admin_edit_coin_field_start(update, context, field, plan_id):
    query = update.callback_query
    prompts = {
        "title": "عنوان جدید را ارسال کنید:",
        "price": "قیمت جدید را به کوین ارسال کنید (فقط عدد):",
        "duration": "مدت اعتبار جدید را به روز ارسال کنید (فقط عدد):",
    }
    if field not in prompts:
        return
    context.user_data['data'] = {"edit_plan_id": plan_id}
    context.user_data['state'] = f"admin_edit_cp_{field}"
    await safe_edit(query, prompts[field], cancel_kb())

@requires_permission("del_coin_plan")
async def admin_del_coin_plan_start(update, context):
    plans = coin_plans()
    if not plans:
        await update.message.reply_text("هیچ کانفیگ کوینی برای حذف ثبت نشده است.")
        return
    kb = [[IKB(f"{p['title']} 🗑", callback_data=f"dcp_{p['id']}", ckey=f"plan:{p['id']}")] for p in plans]
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    await update.message.reply_text("کدام کانفیگ کوین حذف شود؟", reply_markup=InlineKeyboardMarkup(kb))

async def admin_del_coin_plan_ask(update, context, plan_id):
    query = update.callback_query
    conn = get_conn()
    p = conn.execute("SELECT * FROM plans WHERE id=? AND COALESCE(is_coin,0)=1", (plan_id,)).fetchone()
    conn.close()
    if not p:
        await safe_edit(query, "این کانفیگ کوین دیگر وجود ندارد.")
        return
    kb = InlineKeyboardMarkup([
        [IKB("بله، حذف شود ✅", callback_data=f"dcpok_{plan_id}")],
        [IKB("لغو ❌", callback_data="cancel_action")],
    ])
    await safe_edit(query, f"⚠️ «{p['title']}» و تمام کانفیگ‌های داخلش حذف می‌شه. مطمئنید؟", kb)

async def admin_del_coin_plan_do(update, context, plan_id):
    query = update.callback_query
    conn = get_conn()
    conn.execute("DELETE FROM plan_configs WHERE plan_id IN (SELECT id FROM plans WHERE id=? AND COALESCE(is_coin,0)=1)", (plan_id,))
    conn.execute("DELETE FROM plans WHERE id=? AND COALESCE(is_coin,0)=1", (plan_id,))
    conn.commit()
    conn.close()
    await safe_edit(query, "✅ کانفیگ کوین و تمام کانفیگ‌های مرتبط با آن حذف شد.")

# ───────────────────────── ادمین: تنظیمات کوین (زیرمجموعه، روزانه، ماموریت‌ها) ─────────────────────────
def _onoff(flag):
    return "روشن ✅" if flag else "خاموش ❌"

async def render_cs_home(update, context):
    ref_on, daily_on, shop_on = feature_on("referral"), feature_on("daily"), feature_on("coinshop")
    conn = get_conn()
    n_ref = conn.execute("SELECT COUNT(*) c FROM referrals WHERE rewarded=1").fetchone()['c']
    n_missions = conn.execute("SELECT COUNT(*) c FROM coin_missions").fetchone()['c']
    conn.close()
    text = (
        "⚙️ تنظیمات کوین\n\n"
        f"👥 زیرمجموعه‌گیری: {_onoff(ref_on)} — {get_int_setting('referral_reward', 5):,} کوین برای هر نفر (دعوت موفق تا الان: {n_ref:,})\n"
        f"🎁 کوین روزانه: {_onoff(daily_on)} — {get_int_setting('daily_reward', 3):,} کوین در روز (ماموریت‌ها: {n_missions})\n"
        f"🛍 خرید با کوین: {_onoff(shop_on)} — کانفیگ‌های کوین: {len(coin_plans())}\n\n"
        "همه‌ی بخش‌ها به‌صورت پیش‌فرض خاموش هستند و هر وقت خواستید روشنشون کنید."
    )
    kb = InlineKeyboardMarkup([
        [IKB(f"👥 زیرمجموعه‌گیری: {_onoff(ref_on)}", callback_data="cs_tog_referral")],
        [IKB(f"🎁 کوین روزانه: {_onoff(daily_on)}", callback_data="cs_tog_daily")],
        [IKB(f"🛍 خرید با کوین: {_onoff(shop_on)}", callback_data="cs_tog_shop")],
        [IKB("🎯 کوین هر زیرمجموعه", callback_data="cs_refreward"), IKB("🎁 مقدار کوین روزانه", callback_data="cs_dailyreward")],
        [IKB("📋 ماموریت‌های روزانه", callback_data="cs_missions")],
        [IKB("➕ اهدا/کسر کوین کاربر", callback_data="cs_give")],
        [IKB("بستن ❌", callback_data="cancel_action")],
    ])
    await ui(update, text, kb)

@requires_permission("coin_settings")
async def admin_coin_settings_start(update, context):
    await render_cs_home(update, context)

async def render_cs_missions(update, context):
    conn = get_conn()
    ms = conn.execute("SELECT * FROM coin_missions ORDER BY id").fetchall()
    conn.close()
    text = "📋 ماموریت‌های روزانه\n\nکاربر باید همه‌ی ماموریت‌های «روشن» رو انجام بده تا بتونه کوین روزانه رو بگیره."
    if not ms:
        text += "\n\n(هنوز ماموریتی تعریف نشده؛ بدون ماموریت، کاربر مستقیم کوین روزانه رو می‌گیره.)"
    kb = []
    for m in ms:
        kb.append([IKB(f"{'🟢' if m['active'] else '⚪'} {m['title']}", callback_data=f"cs_m_{m['id']}")])
    kb.append([IKB("➕ افزودن ماموریت", callback_data="cs_madd")])
    kb.append([IKB("↩️ بازگشت به تنظیمات کوین", callback_data="cs_home")])
    await ui(update, text, InlineKeyboardMarkup(kb))

async def render_cs_mission(update, context, mission_id):
    conn = get_conn()
    m = conn.execute("SELECT * FROM coin_missions WHERE id=?", (mission_id,)).fetchone()
    conn.close()
    if not m:
        await render_cs_missions(update, context)
        return
    kind = "عضویت در کانال (بررسی خودکار)" if m['kind'] == 'member' else "تایید دستی کاربر (ری‌اکشن و...)"
    text = (
        f"🎯 ماموریت #{m['id']}\n"
        f"وضعیت: {_onoff(m['active'])}\n"
        f"عنوان: {m['title']}\n"
        f"توضیح: {m['description'] or '—'}\n"
        f"لینک: {m['link'] or '—'}\n"
        f"نوع بررسی: {kind}\n"
        f"⏱ زمان انتظار قبل از تایید: {fmt_wait(m['wait_seconds'] or 0)}\n"
        f"کانال: {('@' + m['channel']) if m['channel'] else '—'}"
    )
    kb = InlineKeyboardMarkup([
        [IKB(f"{'⚪ خاموش کردن' if m['active'] else '🟢 روشن کردن'}", callback_data=f"cs_mt_{mission_id}")],
        [IKB("✏️ عنوان", callback_data=f"cs_me_title_{mission_id}"), IKB("✏️ توضیح", callback_data=f"cs_me_desc_{mission_id}")],
        [IKB("🔗 لینک", callback_data=f"cs_me_link_{mission_id}"), IKB("📢 کانال", callback_data=f"cs_me_channel_{mission_id}")],
        [IKB("⏱ زمان انتظار", callback_data=f"cs_me_wait_{mission_id}"), IKB("🔄 تغییر نوع بررسی", callback_data=f"cs_mk_{mission_id}")],
        [IKB("🗑 حذف ماموریت", callback_data=f"cs_md_{mission_id}")],
        [IKB("↩️ برگشت", callback_data="cs_missions")],
    ])
    await ui(update, text, kb)

async def _channel_admin_warning(context, username):
    try:
        me = await context.bot.get_chat_member(f"@{username}", context.bot.id)
        if me.status not in ("administrator", "creator"):
            return "\n⚠️ ربات ادمین این کانال نیست؛ برای بررسی خودکار عضویت باید ربات رو ادمین کانال کنید."
        return ""
    except TelegramError:
        return "\n⚠️ کانال پیدا نشد یا ربات داخلش نیست؛ برای بررسی خودکار عضویت باید ربات رو ادمین کانال کنید."

async def save_new_mission(update, context):
    d = context.user_data.get('data', {})
    conn = get_conn()
    conn.execute(
        "INSERT INTO coin_missions(title, description, link, kind, channel, active, wait_seconds) VALUES (?,?,?,?,?,1,?)",
        (d.get('m_title'), d.get('m_desc') or "", d.get('m_link') or "", d.get('m_kind', 'trust'),
         d.get('m_channel') or "", int(d.get('m_wait') or 0)))
    conn.commit()
    conn.close()
    clear_state(context)
    kb = InlineKeyboardMarkup([[IKB("📋 ماموریت‌ها", callback_data="cs_missions")]])
    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text("✅ ماموریت ذخیره شد و برای کاربران فعال است.", reply_markup=kb)

# ───────────────────────── وضعیت‌های متنی بخش کوین/دسترسی/ریست (ادمین) ─────────────────────────
async def extra_text_states(update, context, state, text) -> bool:
    uid = update.effective_user.id
    d = context.user_data.get('data', {})

    # ── کانفیگ کوین جدید
    if state == "admin_new_cplan_title":
        d['title'] = text
        context.user_data['state'] = "admin_new_cplan_price"
        await update.message.reply_text("برای این کانفیگ چند کوین می‌خواهید؟ (فقط عدد، مثلا 20):", reply_markup=cancel_kb())
        return True
    if state == "admin_new_cplan_price":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("لطفا فقط عدد معتبر (بزرگتر از صفر) ارسال کنید:")
            return True
        d['coin_price'] = int(text)
        context.user_data['state'] = "admin_new_cplan_duration"
        await update.message.reply_text("مدت اعتبار این کانفیگ رو به روز ارسال کنید (فقط عدد، مثلا 30):", reply_markup=cancel_kb())
        return True
    if state == "admin_new_cplan_duration":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("لطفا فقط عدد معتبر (بزرگتر از صفر) ارسال کنید:")
            return True
        if not d.get('title') or not d.get('coin_price'):
            clear_state(context)
            await update.message.reply_text("اطلاعات ناقص بود، دوباره از «افزودن کانفیگ کوین» شروع کنید.")
            return True
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO plans(title, price, duration_days, is_test, category, is_coin, coin_price) VALUES (?,0,?,0,'کوین',1,?)",
            (d['title'], int(text), d['coin_price']))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        title, price = d['title'], d['coin_price']
        context.user_data['data'] = {"edit_plan_id": new_id, "bulk_add": True, "bulk_count": 0}
        await update.message.reply_text(
            f"✅ کانفیگ کوین «{title}» با قیمت {price:,} کوین و {int(text)} روز اعتبار ساخته شد.\n\n"
            "حالا کانفیگ‌های واقعی این پلن رو یکی‌یکی وارد کنید:")
        await prompt_next_bulk_config(update, context)
        return True

    # ── ویرایش کانفیگ کوین
    if state in ("admin_edit_cp_title", "admin_edit_cp_price", "admin_edit_cp_duration"):
        pid = d.get('edit_plan_id')
        field = state.rsplit("_", 1)[-1]
        if field == "title":
            conn = get_conn()
            conn.execute("UPDATE plans SET title=? WHERE id=? AND COALESCE(is_coin,0)=1", (text, pid))
            conn.commit()
            conn.close()
            msg = "✅ عنوان بروزرسانی شد."
        else:
            if not text.isdigit() or int(text) <= 0:
                await update.message.reply_text("لطفا فقط عدد معتبر (بزرگتر از صفر) ارسال کنید:")
                return True
            col = "coin_price" if field == "price" else "duration_days"
            conn = get_conn()
            conn.execute(f"UPDATE plans SET {col}=? WHERE id=? AND COALESCE(is_coin,0)=1", (int(text), pid))
            conn.commit()
            conn.close()
            msg = "✅ قیمت (کوین) بروزرسانی شد." if field == "price" else "✅ مدت اعتبار بروزرسانی شد."
        clear_state(context)
        await update.message.reply_text(msg)
        return True

    # ── تنظیمات کوین
    if state in ("cs_ref_reward", "cs_daily_reward"):
        if not text.isdigit() or int(text) < 0:
            await update.message.reply_text("لطفا فقط عدد معتبر ارسال کنید:")
            return True
        set_setting("referral_reward" if state == "cs_ref_reward" else "daily_reward", int(text))
        clear_state(context)
        await update.message.reply_text("✅ ذخیره شد.")
        await render_cs_home(update, context)
        return True

    if state == "cs_give_userid":
        if not text.isdigit():
            await update.message.reply_text("لطفا فقط آیدی عددی معتبر ارسال کنید:")
            return True
        d['target_id'] = int(text)
        context.user_data['state'] = "cs_give_amount"
        await update.message.reply_text(
            f"موجودی فعلی کوین این کاربر: {get_coins(d['target_id']):,}\n"
            "چند کوین اضافه شود؟ (برای کسر، عدد منفی بفرستید؛ مثلا -5):", reply_markup=cancel_kb())
        return True
    if state == "cs_give_amount":
        t = text.strip()
        if not re.fullmatch(r"-?\d+", t) or int(t) == 0:
            await update.message.reply_text("لطفا یک عدد صحیح غیر صفر ارسال کنید (مثلا 10 یا -5):")
            return True
        amount = int(t)
        target = d['target_id']
        conn = get_conn()
        exists = conn.execute("SELECT 1 FROM users WHERE user_id=?", (target,)).fetchone()
        conn.close()
        if not exists:
            clear_state(context)
            await update.message.reply_text("این کاربر هنوز ربات رو استارت نکرده است ❌")
            return True
        change_coins(target, amount)
        clear_state(context)
        await update.message.reply_text(f"✅ انجام شد. موجودی جدید کوین کاربر {target}: {get_coins(target):,}")
        try:
            if amount > 0:
                await context.bot.send_message(target, f"🪙 {amount:,} کوین به حساب شما اضافه شد.")
            else:
                await context.bot.send_message(target, f"🪙 {abs(amount):,} کوین از حساب شما کسر شد.")
        except TelegramError:
            pass
        return True

    # ── افزودن ماموریت
    if state == "cs_m_title":
        d['m_title'] = text
        context.user_data['state'] = "cs_m_desc"
        await update.message.reply_text("توضیح ماموریت رو بنویسید (مثلا: ۱۰ پست آخر کانال رو ری‌اکشن بزن).\nاگه توضیح نمی‌خواید، - بفرستید:", reply_markup=cancel_kb())
        return True
    if state == "cs_m_desc":
        d['m_desc'] = "" if text.strip() == "-" else text
        context.user_data['state'] = "cs_m_link"
        await update.message.reply_text("لینک ماموریت رو بفرستید (مثلا https://t.me/channel یا @channel).\nاگه لینکی نمی‌خواید، - بفرستید:", reply_markup=cancel_kb())
        return True
    if state == "cs_m_link":
        if text.strip() == "-":
            d['m_link'] = ""
        else:
            link = normalize_link(text)
            if not link:
                await update.message.reply_text("❌ لینک نامعتبره. یه لینک مثل https://t.me/channel یا @channel بفرستید (یا - برای بدون لینک):")
                return True
            d['m_link'] = link
        context.user_data['state'] = "cs_m_kind"
        kb = InlineKeyboardMarkup([
            [IKB("✅ عضویت در کانال (چک خودکار)", callback_data="cs_mtype_member")],
            [IKB("🤝 تایید دستی کاربر (ری‌اکشن و...)", callback_data="cs_mtype_trust")],
            [IKB("لغو ❌", callback_data="cancel_action")],
        ])
        await update.message.reply_text(
            "نوع بررسی ماموریت چیه؟\n\n"
            "✅ عضویت در کانال: ربات خودش عضویت رو چک می‌کنه (ربات باید ادمین کانال باشه).\n"
            "🤝 تایید دستی: کاربر «انجام دادم» رو می‌زنه (برای ری‌اکشن و کارهایی که ربات نمی‌تونه چک کنه).",
            reply_markup=kb)
        return True
    if state == "cs_m_channel":
        username = text.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", username):
            await update.message.reply_text("❌ یوزرنیم کانال نامعتبره. بدون @ و فقط حروف انگلیسی/عدد/آندرلاین بفرستید (مثلا mychannel):")
            return True
        d['m_channel'] = username
        d['m_warn'] = await _channel_admin_warning(context, username)
        if not d.get('m_link'):
            d['m_link'] = f"https://t.me/{username}"
        context.user_data['state'] = "cs_m_wait"
        await update.message.reply_text("⏱ بعد از چند ثانیه/دقیقه کاربر بتونه «انجام دادم» رو بزنه؟\nاگه زودتر بزنه، ربات می‌گه «هنوز انجام ندادی».\n\nمثال: 30 (ثانیه) یا 2m (دو دقیقه) — برای بدون انتظار 0 بفرستید:", reply_markup=cancel_kb())
        return True
    if state == "cs_m_wait":
        secs = parse_wait(text)
        if secs is None:
            await update.message.reply_text("❌ نامعتبره. مثلا 30 یا 2m یا 0 بفرستید:")
            return True
        d['m_wait'] = secs
        warn = d.get('m_warn', "")
        await save_new_mission(update, context)
        if warn:
            await update.message.reply_text(warn.strip())
        return True

    # ── ویرایش ماموریت
    if state == "cs_medit":
        mid, field = d.get('m_id'), d.get('m_field')
        conn = get_conn()
        m = conn.execute("SELECT * FROM coin_missions WHERE id=?", (mid,)).fetchone()
        conn.close()
        if not m:
            clear_state(context)
            await update.message.reply_text("این ماموریت دیگر وجود ندارد.")
            return True
        extra = ""
        if field == "title":
            col, val = "title", text
        elif field == "desc":
            col, val = "description", ("" if text.strip() == "-" else text)
        elif field == "link":
            if text.strip() == "-":
                col, val = "link", ""
            else:
                link = normalize_link(text)
                if not link:
                    await update.message.reply_text("❌ لینک نامعتبره. دوباره بفرستید (یا - برای حذف لینک):")
                    return True
                col, val = "link", link
        elif field == "wait":
            secs = parse_wait(text)
            if secs is None:
                await update.message.reply_text("❌ نامعتبره. مثلا 30 یا 2m یا 0 بفرستید:")
                return True
            col, val = "wait_seconds", secs
        elif field == "channel":
            username = text.strip().lstrip("@")
            if text.strip() == "-":
                col, val = "channel", ""
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", username):
                col, val = "channel", username
                extra = await _channel_admin_warning(context, username)
            else:
                await update.message.reply_text("❌ یوزرنیم کانال نامعتبره. بدون @ بفرستید (یا - برای حذف):")
                return True
        else:
            clear_state(context)
            return True
        conn = get_conn()
        conn.execute(f"UPDATE coin_missions SET {col}=? WHERE id=?", (val, mid))
        conn.commit()
        conn.close()
        clear_state(context)
        await update.message.reply_text("✅ ذخیره شد." + extra)
        await render_cs_mission(update, context, mid)
        return True

    # ── ریست کل ربات (مرحله‌ی دوم: تایپ کلمه‌ی «حذف»)
    if state == "admin_reset_confirm_word":
        if not is_owner(uid):
            clear_state(context)
            return True
        if text.strip() == "حذف":
            await do_reset_bot(update, context)
        else:
            clear_state(context)
            await update.message.reply_text("❎ ریست لغو شد. هیچ اطلاعاتی حذف نشد.", reply_markup=admin_menu_kb(uid))
        return True

    return False


# ═════════════════════════ دسترسی‌های ادمین (UI صفحه‌بندی‌شده) ═════════════════════════
PERM_PAGE_SIZE = 8

async def start_permission_toggle(update, context, target_id, is_new):
    actor_id = update.effective_user.id
    allowed = [p for p in PERMISSIONS if is_owner(actor_id) or has_permission(actor_id, p)]
    context.user_data['state'] = None
    context.user_data['data'] = {
        "perm_target": target_id,
        "perm_is_new": is_new,
        "perm_allowed": allowed,
        "perm_selected": set() if is_new else set(get_admin_permissions(target_id)),
        "perm_page": 0,
    }
    await render_permission_toggle(update, context)

async def render_permission_toggle(update, context):
    d = context.user_data.get('data', {})
    target_id = d['perm_target']
    allowed = d['perm_allowed']
    selected = d['perm_selected']
    pages = max(1, (len(allowed) + PERM_PAGE_SIZE - 1) // PERM_PAGE_SIZE)
    page = min(max(d.get('perm_page', 0), 0), pages - 1)
    d['perm_page'] = page
    chunk = allowed[page * PERM_PAGE_SIZE:(page + 1) * PERM_PAGE_SIZE]
    kb = []
    for p in chunk:
        mark = "✅" if p in selected else "❌"
        kb.append([IKB(f"{mark} {PERMISSIONS[p]}", callback_data=f"permtoggle_{p}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(IKB("◀️ قبلی", callback_data=f"permpage_{page - 1}"))
        nav.append(IKB(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(IKB("بعدی ▶️", callback_data=f"permpage_{page + 1}"))
        kb.append(nav)
    kb.append([IKB("✅ انتخاب همه", callback_data="permall"), IKB("❌ پاک کردن همه", callback_data="permnone")])
    kb.append([IKB("ذخیره ✅", callback_data="permsave")])
    kb.append([IKB("لغو ❌", callback_data="cancel_action")])
    title = "دسترسی‌های ادمین جدید" if d['perm_is_new'] else "ویرایش دسترسی‌های ادمین"
    text = (f"{title} (کاربر {target_id}):\nروی هرکدوم بزنید تا فعال/غیرفعال بشه.\n"
            f"فعال: {len([p for p in allowed if p in selected])} از {len(allowed)}")
    if not allowed:
        text += "\n\n⚠️ شما خودتون هیچ دسترسی‌ای برای واگذاری ندارید."
    await ui(update, text, InlineKeyboardMarkup(kb))

async def cb_permtoggle(update, context, perm):
    d = context.user_data.get('data', {})
    if 'perm_target' not in d or perm not in d.get('perm_allowed', []):
        await qanswer(update.callback_query, "این عملیات منقضی شده یا دسترسی ندارید، دوباره تلاش کنید.", show_alert=True)
        return
    if perm in d['perm_selected']:
        d['perm_selected'].discard(perm)
    else:
        d['perm_selected'].add(perm)
    await render_permission_toggle(update, context)

async def cb_permpage(update, context, page):
    d = context.user_data.get('data', {})
    if 'perm_target' not in d:
        await qanswer(update.callback_query, "این عملیات منقضی شده، دوباره تلاش کنید.", show_alert=True)
        return
    d['perm_page'] = page
    await render_permission_toggle(update, context)

async def cb_perm_all(update, context, select_all):
    d = context.user_data.get('data', {})
    if 'perm_target' not in d:
        await qanswer(update.callback_query, "این عملیات منقضی شده، دوباره تلاش کنید.", show_alert=True)
        return
    if select_all:
        d['perm_selected'] |= set(d['perm_allowed'])
    else:
        d['perm_selected'] -= set(d['perm_allowed'])
    await render_permission_toggle(update, context)

async def cb_permsave(update, context):
    query = update.callback_query
    d = context.user_data.get('data', {})
    if 'perm_target' not in d:
        await qanswer(query, "این عملیات منقضی شده، دوباره تلاش کنید.", show_alert=True)
        return
    target_id = d['perm_target']
    is_new = d['perm_is_new']
    allowed = set(d['perm_allowed'])
    # فقط دسترسی‌هایی که خود ادمین‌دهنده داره قابل تغییره؛ بقیه‌ی دسترسی‌های قبلی دست‌نخورده می‌مونن
    current = set() if is_new else set(get_admin_permissions(target_id))
    final = (current - allowed) | (set(d['perm_selected']) & allowed)
    if is_new:
        conn = get_conn()
        conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (target_id,))
        conn.execute("UPDATE users SET banned=0, ban_reason=NULL WHERE user_id=?", (target_id,))
        conn.commit()
        conn.close()
    set_admin_permissions(target_id, final)
    clear_state(context)
    await safe_edit(query, f"✅ دسترسی‌های کاربر {target_id} ذخیره شد ({len(final)} دسترسی فعال).")
    try:
        if is_new:
            await context.bot.send_message(target_id, "🎉 شما توسط ادمین ربات به عنوان ادمین اضافه شدید. برای شروع /start بزنید.")
        else:
            await context.bot.send_message(target_id, "🔄 دسترسی‌های ادمین شما بروزرسانی شد. برای دیدن منوی جدید /start بزنید.")
    except TelegramError:
        pass

# ═════════════════════════ پنل رنگ دکمه‌ها (همه‌ی دکمه‌ها، صفحه‌بندی‌شده) ═════════════════════════
INLINE_REGISTRY = [
    ("🛒 خرید و پرداخت (دکمه‌های شیشه‌ای کاربر)", [
        ("i:check_join", "عضو شدم، بررسی کن ✅"),
        ("i:joinchannel", "عضویت در کانال اجباری 📢"),
        ("i:discount_yes", "دارم ✅ (کد تخفیف)"),
        ("i:discount_no", "ندارم ❌ (کد تخفیف)"),
        ("i:pay_wallet", "کیف پول 💰 (روش پرداخت)"),
        ("i:pay_card", "کارت به کارت 💳"),
        ("i:charge_wallet", "شارژ کیف پول 💳"),
        ("i:renewplan", "تمدید این پلن 🔄"),
        ("i:vieworder", "مشاهده کانفیگ 👁"),
        ("i:hideorder", "حذف از لیست 🗑"),
        ("i:wheel_spin", "بچرخون 🎡"),
    ]),
    ("↩️ لغو / بستن / پایان / بازگشت", [
        ("i:cancel:لغو ❌", "لغو ❌"),
        ("i:cancel:انصراف ❌", "انصراف ❌"),
        ("i:cancel:بستن ❌", "بستن ❌"),
        ("i:cancel:بستن", "بستن"),
        ("i:cancel:پایان ✅", "پایان ✅"),
        ("i:cancel:بازگشت 🔙", "بازگشت 🔙"),
        ("i:cancel:❌ نه، منصرف شدم", "❌ نه، منصرف شدم"),
    ]),
    ("🪙 کوین (دکمه‌های کاربر)", [
        ("i:coin_ref", "👥 زیرمجموعه‌گیری"),
        ("i:coin_daily", "🎁 دریافت کوین روزانه"),
        ("i:coin_shop", "🛍 خرید با کوین"),
        ("i:coin_home", "↩️ برگشت (بخش کوین)"),
        ("i:coinbuyok", "تایید خرید ✅ (کوین)"),
        ("i:dailydone", "انجام دادم ✅ (ماموریت)"),
        ("i:dailyclaim", "🎁 دریافت کوین"),
        ("i:missionlink", "🔗 لینک ماموریت"),
        ("i:sharelink", "📤 اشتراک‌گذاری لینک دعوت"),
    ]),
    ("🧾 رسیدها و تیکت‌ها (ادمین)", [
        ("i:approve_order", "تایید رسید ✅"),
        ("i:reject_order", "رد رسید ❌"),
        ("i:approve_charge", "تایید شارژ ✅"),
        ("i:reject_charge", "رد شارژ ❌"),
        ("i:reply_ticket", "ارسال پاسخ به کاربر ↩️"),
    ]),
    ("🗂 پلن‌ها و کانفیگ‌ها (ادمین)", [
        ("i:cfgtype_text", "متنی 📝 (کانفیگ پلن)"),
        ("i:cfgtype_photo", "عکس + کپشن 🖼 (کانفیگ پلن)"),
        ("i:bulkcfg_done", "پایان (تعداد اضافه‌شده) ✅"),
        ("i:editfield_title", "تغییر عنوان ✏️"),
        ("i:editfield_price", "تغییر قیمت 💵"),
        ("i:editfield_duration", "تغییر مدت اعتبار 📅"),
        ("i:editfield_category", "تغییر دسته 🏷"),
        ("i:editfield_addcfg", "افزودن کانفیگ به این پلن ➕"),
        ("i:editfield_listcfg", "مشاهده/حذف کانفیگ‌های این پلن 📋"),
        ("i:delcfg", "حذف #N 🗑 (کانفیگ)"),
        ("i:viewcfg", "مشاهده #N 👁 (کانفیگ)"),
    ]),
    ("🪙 کانفیگ کوین و تنظیمات کوین (ادمین)", [
        ("i:ecpf_title", "تغییر عنوان ✏️ (کانفیگ کوین)"),
        ("i:ecpf_price", "تغییر قیمت (کوین) 🪙"),
        ("i:ecpf_duration", "تغییر مدت اعتبار 📅 (کانفیگ کوین)"),
        ("i:dcpok", "بله، حذف شود ✅ (کانفیگ کوین)"),
        ("i:cs_tog_referral", "👥 روشن/خاموش زیرمجموعه‌گیری"),
        ("i:cs_tog_daily", "🎁 روشن/خاموش کوین روزانه"),
        ("i:cs_tog_shop", "🛍 روشن/خاموش خرید با کوین"),
        ("i:cs_refreward", "🎯 کوین هر زیرمجموعه"),
        ("i:cs_dailyreward", "🎁 مقدار کوین روزانه"),
        ("i:cs_missions", "📋 ماموریت‌های روزانه"),
        ("i:cs_give", "➕ اهدا/کسر کوین کاربر"),
        ("i:cs_madd", "➕ افزودن ماموریت"),
        ("i:cs_m", "انتخاب ماموریت (لیست)"),
        ("i:cs_mt", "🟢/⚪ روشن/خاموش ماموریت"),
        ("i:cs_me_title", "✏️ عنوان ماموریت"),
        ("i:cs_me_desc", "✏️ توضیح ماموریت"),
        ("i:cs_me_link", "🔗 لینک ماموریت (ویرایش)"),
        ("i:cs_me_channel", "📢 کانال ماموریت"),
        ("i:cs_me_wait", "⏱ زمان انتظار ماموریت"),
        ("i:cs_mk", "🔄 تغییر نوع بررسی"),
        ("i:cs_md", "🗑 حذف ماموریت"),
        ("i:cs_mdok", "بله، حذف ماموریت ✅"),
        ("i:cs_mtype_member", "✅ ماموریت: عضویت در کانال"),
        ("i:cs_mtype_trust", "🤝 ماموریت: تایید دستی"),
        ("i:cs_home", "↩️ بازگشت به تنظیمات کوین"),
    ]),
    ("🎁 کانفیگ تست (ادمین)", [
        ("i:testtype_text", "متنی 📝 (کانفیگ تست)"),
        ("i:testtype_photo", "عکس + کپشن 🖼 (کانفیگ تست)"),
        ("i:deltest", "حذف #N 🗑 (کانفیگ تست)"),
        ("i:edittest_sel", "#N ✏️ (ویرایش کانفیگ تست)"),
    ]),
    ("🎡 گردونه شانس (ادمین)", [
        ("i:wheel_addprize", "افزودن جایزه ➕"),
        ("i:wheel_listprizes", "لیست/حذف جایزه‌ها 📋"),
        ("i:wheel_setcooldown", "تغییر فاصله زمانی ⏱"),
        ("i:wheeltype_discount", "🏷 کد تخفیف (نوع جایزه)"),
        ("i:wheeltype_config", "🎁 کانفیگ رایگان (نوع جایزه)"),
        ("i:wheeltype_respin", "🔄 یک دور دیگر (نوع جایزه)"),
        ("i:wheeltype_none", "پوچ ❌ (نوع جایزه)"),
        ("i:wheel_wizard_done", "پایان ✅ (تنظیم جایزه‌ها)"),
        ("i:wheel_wizard_continue", "رد شدن ⏭"),
        ("i:wheeldelprize", "حذف #N 🗑 (جایزه)"),
        ("i:wheeladdcfg", "افزودن کانفیگ #N ➕ (جایزه)"),
        ("i:wheelcfgtype_text", "متنی 📝 (کانفیگ جایزه)"),
        ("i:wheelcfgtype_photo", "عکس + کپشن 🖼 (کانفیگ جایزه)"),
    ]),
    ("🏷 کد تخفیف (ادمین)", [
        ("i:discscope_all", "🌐 همه‌ی پلن‌ها"),
        ("i:discscope_cat_حجمی", "حجمی (فقط این دسته)"),
        ("i:discscope_cat_نامحدود", "نامحدود (فقط این دسته)"),
        ("i:discscope_plan", "🎯 فقط یک پلن خاص"),
        ("i:discscope_back", "↩️ برگشت (دامنه‌ی کد)"),
        ("i:revokecode", "باطل کردن کد تخفیف ❌"),
    ]),
    ("👥 کاربران، ادمین‌ها و دسترسی‌ها (ادمین)", [
        ("i:viewuserorders", "مشاهده کانفیگ‌های خریداری‌شده 📦"),
        ("i:viewuser", "مشاهده کاربر 💬"),
        ("i:adminvieworder", "مشاهده کانفیگ 👁 (ادمین)"),
        ("i:unban", "رفع مسدودی ✅"),
        ("i:selnewadmin", "انتخاب کاربر برای ادمین شدن 👤"),
        ("i:editadminperm", "ویرایش دسترسی ادمین 🔑"),
        ("i:removeadmin", "حذف ادمین 🗑"),
        ("i:transferown", "انتقال مالکیت ♻️"),
        ("i:transferownok", "بله، مطمئنم ✅ (انتقال مالکیت)"),
        ("i:delchannel", "حذف کانال اجباری 🗑"),
        ("i:permsave", "ذخیره ✅ (دسترسی‌ها)"),
        ("i:permall", "✅ انتخاب همه (دسترسی‌ها)"),
        ("i:permnone", "❌ پاک کردن همه (دسترسی‌ها)"),
        ("i:permpage", "◀️ قبلی / بعدی ▶️ (صفحه‌ی دسترسی‌ها)"),
    ]),
    ("🧨 ریست ربات", [
        ("i:resetbot", "⚠️ بله، می‌خوام ریست کنم"),
    ]),
]

def _dynamic_color_items():
    items = [(f"category:{c}", f"{CATEGORY_LABELS[c]} (دسته‌بندی)") for c in PLAN_CATEGORIES]
    conn = get_conn()
    plans = conn.execute("SELECT id, title, category, is_test, COALESCE(is_coin,0) is_coin FROM plans ORDER BY is_coin, category, title").fetchall()
    conn.close()
    for p in plans:
        if p['is_coin']:
            kind = "کوین 🪙"
        elif p['is_test']:
            kind = "تست"
        else:
            kind = CATEGORY_LABELS.get(p['category'], p['category'])
        items.append((f"plan:{p['id']}", f"{p['title']} ({kind})"))
    return items

def _admin_menu_color_items():
    items = []
    for row in ADMIN_LAYOUT + OWNER_LAYOUT:
        for t, _slug in row:
            if t == "@MAINT":
                items.extend([(MAINT_OFF_TXT, MAINT_OFF_TXT), (MAINT_ON_TXT, MAINT_ON_TXT)])
            elif t == "@WHEEL":
                items.extend([(WHEEL_OFF_TXT, WHEEL_OFF_TXT), (WHEEL_ON_TXT, WHEEL_ON_TXT)])
            else:
                items.append((t, t))
    items.append((BACK_BTN, BACK_BTN))
    return items

def get_color_groups():
    groups = [
        {"title": "📱 کیبورد اصلی کاربران", "dynamic": False, "refresh": "main",
         "items": lambda: [(t, t) for t in MAIN_MENU_ORDER] + [(BACK_BTN, BACK_BTN)]},
        {"title": "🛠 کیبورد پنل مدیریت", "dynamic": False, "refresh": "admin", "items": _admin_menu_color_items},
        {"title": "🗂 دسته‌ها و پلن‌ها (همه‌ی جاها)", "dynamic": True, "refresh": None, "items": _dynamic_color_items},
    ]
    for title, items in INLINE_REGISTRY:
        groups.append({"title": title, "dynamic": False, "refresh": None, "items": (lambda it=items: list(it))})
    return groups

def get_colorable_buttons():
    """لیست تخت همه‌ی دکمه‌های قابل رنگ‌آمیزی: [(کلید، برچسب)]"""
    out = []
    for g in get_color_groups():
        out.extend(g["items"]())
    return out

async def render_color_home(update, context):
    groups = get_color_groups()
    note = "" if COLORS_SUPPORTED else "\n\n⚠️ نسخه‌ی کتابخونه‌ی ربات قدیمیه و رنگ‌ها نمایش داده نمی‌شن (requirements.txt رو آپدیت کنید)."
    text = ("🎨 تغییر رنگ دکمه‌ها\n\nیک بخش رو انتخاب کنید. رنگ‌ها: قرمز، سبز، آبی و پیش‌فرض.\n"
            "با انتخاب رنگ برای دکمه‌های کیبورد پایین، کیبورد همون لحظه به‌روز می‌شه." + note)
    kb = [[IKB(g["title"], callback_data=f"clr|grp|{i}|0", ckey="")] for i, g in enumerate(groups)]
    kb.append([IKB("🧹 ریست رنگ همه‌ی دکمه‌ها", callback_data="clr|rstask", ckey="")])
    kb.append([IKB("بستن ❌", callback_data="clr|close", ckey="")])
    await ui(update, text, InlineKeyboardMarkup(kb))

COLOR_PAGE_SIZE = 8

async def render_color_group(update, context, gi, pg):
    groups = get_color_groups()
    if gi < 0 or gi >= len(groups):
        await render_color_home(update, context)
        return
    g = groups[gi]
    items = g["items"]()
    pages = max(1, (len(items) + COLOR_PAGE_SIZE - 1) // COLOR_PAGE_SIZE)
    pg = min(max(pg, 0), pages - 1)
    chunk = items[pg * COLOR_PAGE_SIZE:(pg + 1) * COLOR_PAGE_SIZE]
    kb = []
    for j, (key, label) in enumerate(chunk):
        idx = pg * COLOR_PAGE_SIZE + j
        token = key if g["dynamic"] else str(idx)
        icon = COLOR_ICONS.get(get_button_color(key), "⚪")
        kb.append([IKB(f"{icon} {label}", callback_data=f"clr|pick|{gi}|{pg}|{token}", ckey="")])
    if pages > 1:
        nav = []
        if pg > 0:
            nav.append(IKB("◀️ قبلی", callback_data=f"clr|grp|{gi}|{pg - 1}", ckey=""))
        nav.append(IKB(f"{pg + 1}/{pages}", callback_data="noop", ckey=""))
        if pg < pages - 1:
            nav.append(IKB("بعدی ▶️", callback_data=f"clr|grp|{gi}|{pg + 1}", ckey=""))
        kb.append(nav)
    kb.append([IKB("↩️ بخش‌ها", callback_data="clr|home", ckey="")])
    await ui(update, f"{g['title']}\n\nدکمه موردنظر رو انتخاب کنید:", InlineKeyboardMarkup(kb))

def _resolve_color_item(gi, token):
    groups = get_color_groups()
    if gi < 0 or gi >= len(groups):
        return None
    g = groups[gi]
    items = g["items"]()
    if g["dynamic"]:
        for key, label in items:
            if key == token:
                return g, key, label
        return None
    try:
        key, label = items[int(token)]
    except (ValueError, IndexError):
        return None
    return g, key, label

async def render_color_pick(update, context, gi, pg, token):
    res = _resolve_color_item(gi, token)
    if not res:
        await qanswer(update.callback_query, "دکمه نامعتبر یا حذف شده است.", show_alert=True)
        await render_color_group(update, context, gi, pg)
        return
    g, key, label = res
    current = get_button_color(key)
    rows = [
        [IKB("🔴 قرمز", callback_data=f"clr|set|{gi}|{pg}|{token}|danger", ckey="", style="danger"),
         IKB("🟢 سبز", callback_data=f"clr|set|{gi}|{pg}|{token}|success", ckey="", style="success")],
        [IKB("🔵 آبی", callback_data=f"clr|set|{gi}|{pg}|{token}|primary", ckey="", style="primary"),
         IKB("⚪ پیش‌فرض", callback_data=f"clr|set|{gi}|{pg}|{token}|default", ckey="")],
        [IKB("↩️ برگشت", callback_data=f"clr|grp|{gi}|{pg}", ckey="")],
    ]
    await ui(update, f"دکمه: {label}\nرنگ فعلی: {COLOR_NAMES.get(current, current)}\n\nرنگ جدید را انتخاب کنید:",
             InlineKeyboardMarkup(rows))

async def cb_color_set(update, context, gi, pg, token, color):
    query = update.callback_query
    res = _resolve_color_item(gi, token)
    if not res or color not in COLOR_NAMES:
        await qanswer(query, "دکمه نامعتبر یا حذف شده است.", show_alert=True)
        return
    g, key, label = res
    set_button_color(key, color)
    await qanswer(query, "رنگ ذخیره شد ✅")
    await render_color_pick(update, context, gi, pg, token)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if g.get("refresh") == "main":
        await context.bot.send_message(chat_id, f"🔄 کیبورد با رنگ جدید «{label}» به‌روز شد.", reply_markup=main_menu_kb(uid))
    elif g.get("refresh") == "admin":
        await context.bot.send_message(chat_id, f"🔄 کیبورد با رنگ جدید «{label}» به‌روز شد.", reply_markup=admin_menu_kb(uid))

@owner_only
async def admin_button_color_start(update, context):
    await render_color_home(update, context)

async def cb_color_reset_ask(update, context):
    kb = InlineKeyboardMarkup([
        [IKB("بله، همه‌ی رنگ‌ها ریست بشه ✅", callback_data="clr|rstok", ckey="")],
        [IKB("↩️ برگشت", callback_data="clr|home", ckey="")],
    ])
    await ui(update, "🧹 رنگ همه‌ی دکمه‌ها (کیبورد معمولی و شیشه‌ای) به حالت پیش‌فرض برمی‌گرده. مطمئنید؟", kb)

async def cb_color_reset_do(update, context):
    reset_all_button_colors()
    uid = update.effective_user.id
    await ui(update, "✅ رنگ همه‌ی دکمه‌ها ریست شد.",
             InlineKeyboardMarkup([[IKB("↩️ بخش‌ها", callback_data="clr|home", ckey="")]]))
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id, "🔄 کیبورد به‌روز شد.", reply_markup=admin_menu_kb(uid))

# ═════════════════════════ بکاپ، بازگردانی و ریست کل ربات ═════════════════════════
def make_db_snapshot(dest_path):
    """یه کپی کامل و سالم (حتی وسط نوشتن) از دیتابیس می‌سازه."""
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

@owner_only
async def admin_backup_start(update, context):
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("فایل دیتابیس پیدا نشد ❌")
        return
    status_msg = await update.message.reply_text("⏳ در حال آماده‌سازی فایل بکاپ...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = f"{DB_PATH}.manual_tmp"
    try:
        make_db_snapshot(snap)
        size = os.path.getsize(snap)
        if size > 49 * 1024 * 1024:
            await update.message.reply_text("⚠️ حجم دیتابیس از ۵۰ مگابایت بیشتره و تلگرام اجازه‌ی ارسالش رو نمی‌ده.")
            return
        with open(snap, "rb") as f:
            await update.message.reply_document(
                document=f, filename=f"backup_{ts}.db",
                caption="💾 فایل بکاپ کامل دیتابیس ربات.\nاین فایل رو یه جای امن نگه‌دار — برای بازگردانی از دکمه «بازگردانی از بکاپ 📥» استفاده کن."
            )
    except Exception as e:
        logger.exception("manual backup failed")
        await update.message.reply_text(f"خطا در ساخت/ارسال بکاپ: {e}")
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass
        try:
            await status_msg.delete()
        except Exception:
            pass

@owner_only
async def admin_restore_start(update, context):
    context.user_data['state'] = "admin_restore_wait_file"
    context.user_data['data'] = {}
    await update.message.reply_text(
        "⚠️ توجه: با ارسال فایل بکاپ، تمام اطلاعات فعلی ربات (کاربران، پلن‌ها، سفارش‌ها، کیف پول‌ها و...) با اطلاعات داخل فایل جایگزین می‌شه و قابل بازگشت نیست.\n\n"
        "فایل بکاپ (.db) رو ارسال کنید:",
        reply_markup=cancel_kb()
    )

def validate_sqlite_file(path):
    """(ok, message) — بررسی می‌کنه فایل یه دیتابیس سالمِ این ربات باشه."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        if not header.startswith(b"SQLite format 3"):
            return False, "❌ فایل ارسالی یک فایل دیتابیس معتبر (sqlite) نیست."
        conn = sqlite3.connect(path)
        try:
            res = conn.execute("PRAGMA integrity_check").fetchone()
            if not res or res[0] != "ok":
                return False, "❌ فایل بکاپ خراب است (integrity_check ناموفق)."
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            conn.close()
        if not {"users", "plans", "admins"} <= tables:
            return False, "❌ این فایل مربوط به این ربات نیست (جدول‌های اصلی پیدا نشد)."
        return True, "ok"
    except Exception as e:
        return False, f"خطا در بررسی فایل: {e}"

async def document_router(update, context):
    user = update.effective_user
    if not is_owner(user.id):
        return
    if context.user_data.get('state') != "admin_restore_wait_file":
        return
    doc = update.message.document
    if not doc:
        return
    await update.message.reply_text("⏳ در حال دریافت و بررسی فایل...")
    tmp_path = DB_PATH + ".restore_tmp"
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        await update.message.reply_text(f"خطا در دریافت فایل: {e}")
        return
    ok, msg = validate_sqlite_file(tmp_path)
    if not ok:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        clear_state(context)
        await update.message.reply_text(msg)
        return
    try:
        if os.path.exists(DB_PATH):
            shutil.copyfile(DB_PATH, DB_PATH + ".before_restore.bak")
        os.replace(tmp_path, DB_PATH)
    except Exception as e:
        await update.message.reply_text(f"خطا در جایگزینی فایل: {e}")
        return
    invalidate_color_cache()
    try:
        init_db()
    except Exception as e:
        await update.message.reply_text(f"⚠️ بازگردانی انجام شد ولی به‌روزرسانی ساختار دیتابیس با خطا مواجه شد: {e}")
        return
    clear_state(context)
    await update.message.reply_text(
        "✅ اطلاعات با موفقیت بازگردانی شد.\n"
        "(نسخه‌ی قبل از بازگردانی هم با پسوند .before_restore.bak کنار دیتابیس نگه داشته شده.)",
        reply_markup=admin_menu_kb(user.id)
    )

@owner_only
async def admin_reset_start(update, context):
    kb = InlineKeyboardMarkup([
        [IKB("⚠️ بله، می‌خوام ریست کنم", callback_data="resetbot_1")],
        [IKB("❌ نه، منصرف شدم", callback_data="cancel_action")],
    ])
    await update.message.reply_text(
        "🧨 ریست کل ربات\n\n"
        "با این کار «همه‌چیز» پاک می‌شه: کاربران، کیف پول‌ها، کوین‌ها، پلن‌ها و کانفیگ‌ها، سفارش‌ها، کدهای تخفیف، "
        "ادمین‌ها و دسترسی‌ها، تنظیمات، رنگ دکمه‌ها و ...\n\n"
        "واقعا می‌خوای ربات رو ریست کنی؟",
        reply_markup=kb
    )

async def cb_reset_step1(update, context):
    context.user_data['state'] = "admin_reset_confirm_word"
    context.user_data['data'] = {}
    kb = InlineKeyboardMarkup([[IKB("❌ نه، منصرف شدم", callback_data="cancel_action")]])
    await ui(update,
             "🚨 آخرین هشدار!\n\nاین کار قابل بازگشت نیست و تمام اطلاعات ربات برای همیشه پاک می‌شه "
             "(قبلش یه فایل بکاپ برات می‌فرستم).\n\n"
             "واقعا می‌خوای حذف کنی؟ اگه مطمئنی، همین الان کلمه‌ی «حذف» رو بفرست.\n"
             "هر پیام دیگه‌ای = لغو.", kb)

async def do_reset_bot(update, context):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    # ۱) بکاپ ایمنی برای مالک
    try:
        snap = f"{DB_PATH}.reset_backup_tmp"
        make_db_snapshot(snap)
        with open(snap, "rb") as f:
            await context.bot.send_document(
                chat_id, document=f, filename=f"before_reset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                caption="💾 بکاپ ایمنی قبل از ریست (اگه پشیمون شدی از «بازگردانی از بکاپ» استفاده کن).")
        os.remove(snap)
    except Exception as e:
        logger.warning(f"safety backup before reset failed: {e}")
        await context.bot.send_message(chat_id, f"⚠️ ساخت بکاپ ایمنی ناموفق بود ({e}). ریست ادامه پیدا می‌کنه.")
    # ۲) حذف همه‌ی جدول‌ها و ساخت دوباره
    conn = get_conn()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    for t in tables:
        conn.execute(f'DROP TABLE IF EXISTS "{t}"')
    conn.commit()
    conn.close()
    invalidate_color_cache()
    init_db()
    clear_state(context)
    await context.bot.send_message(
        chat_id,
        "✅ ربات کاملاً ریست شد و مثل روز اول شد.\n(مالک‌ها دوباره از روی متغیر ADMIN_IDS ساخته شدن؛ کوین‌ها، زیرمجموعه‌گیری و کوین روزانه خاموش هستند.)",
        reply_markup=main_menu_kb(uid)
    )

# ═════════════════════════ روتر دکمه‌های پنل مدیریت (کیبورد معمولی) ═════════════════════════
async def admin_menu_dispatch(update, context, text):
    slug = TEXT_TO_SLUG.get(text)
    handlers = {
        "add_plan": admin_add_plan_start,
        "edit_plan": admin_edit_plan_start,
        "del_plan": admin_del_plan_start,
        "add_test": admin_add_test_start,
        "list_test": admin_list_test_configs,
        "edit_test": admin_edit_test_start,
        "del_test": admin_delete_test_start,
        "add_coin_plan": admin_add_coin_plan_start,
        "edit_coin_plan": admin_edit_coin_plan_start,
        "del_coin_plan": admin_del_coin_plan_start,
        "list_coin_plan": admin_list_coin_plans,
        "coin_settings": admin_coin_settings_start,
        "users": admin_show_users,
        "direct_msg": admin_direct_start,
        "ban": admin_ban_start,
        "unban": admin_unban_list,
        "charge_wallet": admin_chargewallet_start,
        "dec_wallet": admin_decrease_wallet_start,
        "broadcast": admin_broadcast_start,
        "new_discount": admin_add_discount_start,
        "revoke_discount": admin_revoke_discount_start,
        "add_channel": admin_addchannel_start,
        "del_channel": admin_delchannel_start,
        "change_card": admin_change_card_start,
        "welcome": admin_change_welcome_start,
        "maintenance": admin_maintenance_toggle_start,
        "add_admin": admin_add_admin_start,
        "list_admins": admin_list_admins,
        "owner_del_admin": admin_del_admin_start,
        "owner_edit_perms": admin_edit_permissions_start,
        "owner_transfer": admin_transfer_ownership_start,
        "owner_wheel_toggle": admin_wheel_toggle,
        "owner_wheel_manage": admin_wheel_manage,
        "owner_backup": admin_backup_start,
        "owner_restore": admin_restore_start,
        "owner_colors": admin_button_color_start,
        "owner_reset": admin_reset_start,
    }
    func = handlers.get(slug)
    if not func:
        return
    user_id = update.effective_user.id
    if slug in OWNER_SLUGS:
        if not is_owner(user_id):
            await update.message.reply_text("این قابلیت فقط برای مالک اصلی ربات است ❌")
            return
    elif not has_permission(user_id, slug):
        await update.message.reply_text(
            f"⛔️ شما دسترسی «{PERMISSIONS.get(slug, slug)}» رو ندارید.\nاز مالک ربات بخواهید این دسترسی رو بهتون بده.")
        return
    await func(update, context)

# ═════════════════════════ روتر دکمه‌های شیشه‌ای بخش‌های جدید ═════════════════════════
async def extra_callback_router(update, context, data) -> bool:
    """True یعنی این callback توسط بخش‌های جدید مدیریت شد."""
    query = update.callback_query
    uid = update.effective_user.id

    if data == "noop":
        return True

    # ── کوین (کاربر)
    if data == "coin_home":
        await show_coin_home(update, context)
        return True
    if data == "coin_ref":
        await cb_coin_ref(update, context)
        return True
    if data == "coin_daily":
        await cb_coin_daily(update, context)
        return True
    if data == "coin_shop":
        await cb_coin_shop(update, context)
        return True
    if data.startswith("coinplan_"):
        await cb_coin_plan_confirm(update, context, int(data.split("_")[1]))
        return True
    if data.startswith("coinbuyok_"):
        await cb_coin_buy(update, context, int(data.split("_")[1]))
        return True
    if data.startswith("dailydone_"):
        await cb_daily_done(update, context, int(data.split("_")[1]))
        return True
    if data == "dailyclaim":
        await cb_daily_claim(update, context)
        return True

    # ── کانفیگ کوین (ادمین)
    if data.startswith("ecpf_"):
        _, field, pid = data.split("_")
        await admin_edit_coin_field_start(update, context, field, int(pid))
        return True
    if data.startswith("ecp_"):
        await admin_edit_coin_plan_menu(update, context, int(data.split("_")[1]))
        return True
    if data.startswith("dcpok_"):
        await admin_del_coin_plan_do(update, context, int(data.split("_")[1]))
        return True
    if data.startswith("dcp_"):
        await admin_del_coin_plan_ask(update, context, int(data.split("_")[1]))
        return True

    # ── تنظیمات کوین (ادمین)
    if data.startswith("cs_"):
        if data == "cs_home":
            clear_state(context)
            await render_cs_home(update, context)
        elif data in ("cs_tog_referral", "cs_tog_daily", "cs_tog_shop"):
            name = {"cs_tog_referral": "referral", "cs_tog_daily": "daily", "cs_tog_shop": "coinshop"}[data]
            set_feature(name, not feature_on(name))
            await render_cs_home(update, context)
        elif data == "cs_refreward":
            context.user_data['state'] = "cs_ref_reward"
            context.user_data['data'] = {}
            await safe_edit(query, "به ازای هر زیرمجموعه چند کوین داده بشه؟ (فقط عدد، مثلا 5):", cancel_kb())
        elif data == "cs_dailyreward":
            context.user_data['state'] = "cs_daily_reward"
            context.user_data['data'] = {}
            await safe_edit(query, "هر روز چند کوین داده بشه؟ (فقط عدد، مثلا 3):", cancel_kb())
        elif data == "cs_give":
            context.user_data['state'] = "cs_give_userid"
            context.user_data['data'] = {}
            await safe_edit(query, "آیدی عددی کاربر رو ارسال کنید:", cancel_kb())
        elif data == "cs_missions":
            clear_state(context)
            await render_cs_missions(update, context)
        elif data == "cs_madd":
            context.user_data['state'] = "cs_m_title"
            context.user_data['data'] = {}
            await safe_edit(query, "عنوان ماموریت رو بفرستید (مثلا: عضویت در کانال اسپانسر):", cancel_kb())
        elif data in ("cs_mtype_member", "cs_mtype_trust"):
            d = context.user_data.get('data', {})
            if context.user_data.get('state') != "cs_m_kind" or not d.get('m_title'):
                await qanswer(query, "این عملیات منقضی شده، دوباره از «افزودن ماموریت» شروع کنید.", show_alert=True)
                return True
            if data == "cs_mtype_member":
                d['m_kind'] = 'member'
                context.user_data['state'] = "cs_m_channel"
                await safe_edit(query, "یوزرنیم کانال رو بدون @ بفرستید (مثلا mychannel).\n⚠️ ربات باید ادمین اون کانال باشه تا عضویت رو چک کنه:", cancel_kb())
            else:
                d['m_kind'] = 'trust'
                context.user_data['state'] = "cs_m_wait"
                await safe_edit(query, "⏱ بعد از چند ثانیه/دقیقه کاربر بتونه «انجام دادم» رو بزنه؟\nاگه زودتر بزنه، ربات می‌گه «هنوز انجام ندادی».\n\nمثال: 30 (ثانیه) یا 2m (دو دقیقه) — برای بدون انتظار 0 بفرستید:", cancel_kb())
        elif data.startswith("cs_me_"):
            _, _, field, mid = data.split("_")
            context.user_data['state'] = "cs_medit"
            context.user_data['data'] = {"m_id": int(mid), "m_field": field}
            hints = {"title": "عنوان جدید رو بفرستید:", "desc": "توضیح جدید رو بفرستید (- برای خالی کردن):",
                     "link": "لینک جدید رو بفرستید (- برای حذف لینک):",
                     "wait": "زمان انتظار جدید رو بفرستید (مثلا 30 یا 2m؛ 0 = بدون انتظار):", "channel": "یوزرنیم کانال رو بدون @ بفرستید (- برای حذف):"}
            await safe_edit(query, hints.get(field, "مقدار جدید رو بفرستید:"), cancel_kb())
        elif data.startswith("cs_mt_"):
            mid = int(data.split("_")[2])
            conn = get_conn()
            conn.execute("UPDATE coin_missions SET active = 1 - active WHERE id=?", (mid,))
            conn.commit()
            conn.close()
            await render_cs_mission(update, context, mid)
        elif data.startswith("cs_mk_"):
            mid = int(data.split("_")[2])
            conn = get_conn()
            m = conn.execute("SELECT * FROM coin_missions WHERE id=?", (mid,)).fetchone()
            if m:
                if m['kind'] == 'member':
                    conn.execute("UPDATE coin_missions SET kind='trust' WHERE id=?", (mid,))
                elif not m['channel']:
                    conn.close()
                    await qanswer(query, "برای بررسی خودکار، اول باید «📢 کانال» رو ثبت کنید.", show_alert=True)
                    return True
                else:
                    conn.execute("UPDATE coin_missions SET kind='member' WHERE id=?", (mid,))
                conn.commit()
            conn.close()
            await render_cs_mission(update, context, mid)
        elif data.startswith("cs_mdok_"):
            mid = int(data.split("_")[2])
            conn = get_conn()
            conn.execute("DELETE FROM coin_missions WHERE id=?", (mid,))
            conn.execute("DELETE FROM mission_done WHERE mission_id=?", (mid,))
            conn.commit()
            conn.close()
            await render_cs_missions(update, context)
        elif data.startswith("cs_md_"):
            mid = int(data.split("_")[2])
            kb = InlineKeyboardMarkup([
                [IKB("بله، حذف ماموریت ✅", callback_data=f"cs_mdok_{mid}")],
                [IKB("↩️ برگشت", callback_data=f"cs_m_{mid}", ckey="i:cs_m")],
            ])
            await safe_edit(query, "⚠️ این ماموریت حذف بشه؟", kb)
        elif data.startswith("cs_m_"):
            await render_cs_mission(update, context, int(data.split("_")[2]))
        else:
            return False
        return True

    # ── دسترسی‌ها
    if data.startswith("permtoggle_"):
        await cb_permtoggle(update, context, data[len("permtoggle_"):])
        return True
    if data.startswith("permpage_"):
        await cb_permpage(update, context, int(data.split("_")[1]))
        return True
    if data == "permall":
        await cb_perm_all(update, context, True)
        return True
    if data == "permnone":
        await cb_perm_all(update, context, False)
        return True
    if data == "permsave":
        await cb_permsave(update, context)
        return True

    # ── رنگ دکمه‌ها (مالک)
    if data.startswith("clr|"):
        parts = data.split("|")
        act = parts[1]
        if act == "home":
            await render_color_home(update, context)
        elif act == "grp":
            await render_color_group(update, context, int(parts[2]), int(parts[3]))
        elif act == "pick":
            await render_color_pick(update, context, int(parts[2]), int(parts[3]), parts[4])
        elif act == "set":
            await cb_color_set(update, context, int(parts[2]), int(parts[3]), parts[4], parts[5])
        elif act == "rstask":
            await cb_color_reset_ask(update, context)
        elif act == "rstok":
            await cb_color_reset_do(update, context)
        elif act == "close":
            await safe_edit(query, "پنل تغییر رنگ بسته شد.")
        else:
            return False
        return True

    # ── ریست کل ربات (مالک)
    if data == "resetbot_1":
        await cb_reset_step1(update, context)
        return True

    return False

# ═════════════════════════ هندلر خطای سراسری ═════════════════════════
async def error_handler(update, context):
    err = context.error
    if isinstance(err, BadRequest) and "not modified" in str(err).lower():
        return
    if isinstance(err, Conflict):
        logger.error("⚠️ Conflict: یک نمونه‌ی دیگه از همین ربات هم در حال اجراست (با همین توکن). فقط یکی باید اجرا باشه.")
        return
    logger.error("Unhandled exception", exc_info=err)
    try:
        if update is not None and getattr(update, "effective_message", None) and not getattr(update, "callback_query", None):
            await update.effective_message.reply_text("⚠️ مشکلی پیش اومد، لطفا دوباره تلاش کنید.")
    except Exception:
        pass


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, photo_router))
    app.add_handler(MessageHandler(filters.VIDEO, video_router))
    app.add_handler(MessageHandler(filters.VOICE, voice_router))
    app.add_handler(MessageHandler(filters.Document.ALL, document_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(error_handler)

    if app.job_queue is not None:
        app.job_queue.run_repeating(hourly_backup_job, interval=21600, first=21600)
    else:
        logger.warning("job_queue فعال نیست — برای بکاپ خودکار باید pip install \"python-telegram-bot[job-queue]\" رو اجرا کنید.")

    logger.info("ربات در حال اجراست...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
