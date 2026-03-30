import telebot
from telebot import types
import time
import os
import json
import sqlite3
import threading
import queue
import logging

# ------------------- منطقة الإعدادات -------------------
BOT_TOKEN = "8690606833:AAF3Zuz6HQDtZHkc9I2WMDJyQ777kD2WPFQ"

# 👑 المالك الأساسي (Super Admin) - صلاحيات مطلقة ولا يمكن حذفه
ADMIN_IDS = [865208617, 1783505330] 

# الإعدادات الافتراضية
DEFAULT_VIP_CHANNEL_ID = -1003860710146
DEFAULT_FORCE_SUB_CHANNELS = [
    {"id": -1003795228064, "link": "https://t.me/+cc3vY0Cd9LRkYmVk", "name": "القناة الأولى"},
    {"id": -1003886752757, "link": "https://t.me/+_8zVGBX9OkYwNTFk", "name": "القناة الثانية"},
]

QUALITY_ORDER = ["144p", "240p", "360p", "480p", "720p", "1080p", "4k"]
QUALITY_LABELS = {
    "144p": "144p ☁️", "240p": "240p 📱", "360p": "360p 🎥",
    "480p": "480p 📺", "720p": "720p ᴴᴰ", "1080p": "1080p ᶠᴴᴰ", "4k": "4K ᵁᴴᴰ"
}

DB_PATH = 'bot_database.db'
DB_TIMEOUT = 10
# اتصال PostgreSQL عبر متغير البيئة (مثال: postgresql://user:pass@host:5432/dbname)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BROADCAST_WORKERS = 4
BROADCAST_DELAY = 0.15


def uses_postgres():
    return DATABASE_URL.startswith(("postgres://", "postgresql://"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

def sort_episode_keys(keys):
    def order(k):
        try:
            return (0, int(str(k).strip()))
        except ValueError:
            return (1, str(k))
    return sorted(keys, key=order)

def sort_season_keys(keys):
    def order(k):
        s = str(k).strip()
        if s.startswith("الموسم"):
            tail = s[len("الموسم"):].strip()
            try:
                return (0, int(tail))
            except ValueError:
                return (1, s)
        try:
            return (0, int(s))
        except ValueError:
            return (1, s)
    return sorted(keys, key=order)

def is_user_selecting_season(m):
    t = getattr(m, "text", None)
    if not t:
        return False
    cid = m.chat.id
    if cid not in user_state or "series" not in user_state[cid]:
        return False
    series = user_state[cid]["series"]
    seasons = load_data().get(series, {}).get("seasons", {})
    return t in seasons

user_state = {}
admin_steps = {}

bot = telebot.TeleBot(BOT_TOKEN)
try: bot.remove_webhook()
except: pass

# =======================================================
# 🗄️ نظام قاعدة البيانات (SQLite محلي | PostgreSQL عبر DATABASE_URL)
# =======================================================
def _q(sql):
    return sql.replace("?", "%s") if uses_postgres() else sql


def db_connect():
    if uses_postgres():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)


def init_db():
    conn = db_connect()
    c = conn.cursor()
    if uses_postgres():
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                is_muted INTEGER NOT NULL DEFAULT 0
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS admins (
                admin_id BIGINT PRIMARY KEY,
                name TEXT,
                role TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT NOT NULL,
                series_name TEXT NOT NULL,
                PRIMARY KEY (user_id, series_name)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS ratings (
                user_id BIGINT NOT NULL,
                series_name TEXT NOT NULL,
                rating INTEGER NOT NULL,
                PRIMARY KEY (user_id, series_name)
            )"""
        )
    else:
        c.execute(
            '''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_muted INTEGER DEFAULT 0)'''
        )
        c.execute('''CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins (admin_id INTEGER PRIMARY KEY, name TEXT, role TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorites (user_id INTEGER NOT NULL, series_name TEXT NOT NULL, PRIMARY KEY (user_id, series_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS ratings (user_id INTEGER NOT NULL, series_name TEXT NOT NULL, rating INTEGER NOT NULL, PRIMARY KEY (user_id, series_name))''')
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    try:
        c.execute("ALTER TABLE admins ADD COLUMN name TEXT DEFAULT 'بدون اسم'")
    except Exception:
        pass
    conn.commit()
    conn.close()


def get_setting(key, default_value=None):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT value FROM store WHERE key=?"), (key,))
        row = c.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
        return default_value
    except Exception:
        return default_value


def set_setting(key, value):
    try:
        conn = db_connect()
        c = conn.cursor()
        payload = json.dumps(value, ensure_ascii=False)
        if uses_postgres():
            c.execute(
                """INSERT INTO store (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (key, payload),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO store (key, value) VALUES (?, ?)",
                (key, payload),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass

init_db()
logging.info(
    "قاعدة البيانات: %s",
    "PostgreSQL" if uses_postgres() else f"SQLite ({DB_PATH})",
)
if get_setting('vip_channel') is None: set_setting('vip_channel', DEFAULT_VIP_CHANNEL_ID)
if get_setting('force_channels') is None: set_setting('force_channels', DEFAULT_FORCE_SUB_CHANNELS)

def load_data(): return get_setting('series_data', {})
def save_data(data): set_setting('series_data', data)

def save_user(user_id):
    try:
        conn = db_connect()
        c = conn.cursor()
        if uses_postgres():
            c.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                (user_id,),
            )
        else:
            c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def is_muted(user_id):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT is_muted FROM users WHERE user_id=?"), (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] == 1 if row else False
    except Exception:
        return False


def toggle_mute_status(user_id):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT is_muted FROM users WHERE user_id=?"), (user_id,))
        row = c.fetchone()
        if row:
            new_status = 0 if row[0] == 1 else 1
            c.execute(
                _q("UPDATE users SET is_muted=? WHERE user_id=?"),
                (new_status, user_id),
            )
        else:
            new_status = 1
            c.execute(
                _q("INSERT INTO users (user_id, is_muted) VALUES (?, ?)"),
                (user_id, new_status),
            )
        conn.commit()
        conn.close()
        return "unmuted" if new_status == 0 else "muted"
    except Exception:
        return "unmuted"


def admin_save_confirm_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("✅ تأكيد الحفظ"),
        types.KeyboardButton("↩️ إعادة الإدخال"),
    )
    markup.add(types.KeyboardButton("❌ إلغاء"))
    return markup


def sort_existing_qualities(quality_keys):
    keys_set = set(quality_keys)
    ordered = [q for q in QUALITY_ORDER if q in keys_set]
    extra = sorted(k for k in keys_set if k not in QUALITY_ORDER)
    return ordered + extra


def search_series_names(query, limit=25):
    data = load_data()
    q = (query or "").strip().lower()
    if not q:
        return []
    results = [name for name in data.keys() if q in name.lower()]
    return sort_season_keys(results)[:limit]


def is_favorite(user_id, series_name):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT 1 FROM favorites WHERE user_id=? AND series_name=?"), (user_id, series_name))
        row = c.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def add_favorite(user_id, series_name):
    try:
        conn = db_connect()
        c = conn.cursor()
        if uses_postgres():
            c.execute(
                "INSERT INTO favorites (user_id, series_name) VALUES (%s, %s) ON CONFLICT (user_id, series_name) DO NOTHING",
                (user_id, series_name),
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO favorites (user_id, series_name) VALUES (?, ?)",
                (user_id, series_name),
            )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def remove_favorite(user_id, series_name):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("DELETE FROM favorites WHERE user_id=? AND series_name=?"), (user_id, series_name))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def get_user_favorites(user_id):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT series_name FROM favorites WHERE user_id=? ORDER BY series_name ASC"), (user_id,))
        rows = c.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def set_series_rating(user_id, series_name, rating):
    try:
        value = int(rating)
        if value < 1 or value > 5:
            return False
        conn = db_connect()
        c = conn.cursor()
        if uses_postgres():
            c.execute(
                """INSERT INTO ratings (user_id, series_name, rating) VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, series_name) DO UPDATE SET rating = EXCLUDED.rating""",
                (user_id, series_name, value),
            )
        else:
            c.execute(
                "INSERT OR REPLACE INTO ratings (user_id, series_name, rating) VALUES (?, ?, ?)",
                (user_id, series_name, value),
            )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def get_user_series_rating(user_id, series_name):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT rating FROM ratings WHERE user_id=? AND series_name=?"), (user_id, series_name))
        row = c.fetchone()
        conn.close()
        return int(row[0]) if row else None
    except Exception:
        return None


def get_series_rating_stats(series_name):
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT COUNT(*), AVG(rating) FROM ratings WHERE series_name=?"), (series_name,))
        row = c.fetchone()
        conn.close()
        if not row or row[0] == 0:
            return 0, 0.0
        return int(row[0]), float(row[1] or 0)
    except Exception:
        return 0, 0.0

# =======================================================
# 🔐 نظام إدارة الصلاحيات (RBAC)
# =======================================================
def is_super_admin(user_id): return user_id in ADMIN_IDS

def get_admin_role(user_id):
    if is_super_admin(user_id): return "super"
    try:
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("SELECT role FROM admins WHERE admin_id=?"), (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def is_admin(user_id): return get_admin_role(user_id) is not None

def has_perm(user_id, perm):
    role = get_admin_role(user_id)
    if role == "super": return True
    if role == "👑 مشرف عام": return True
    if perm == "add" and role == "📤 مسؤول نشر": return True
    if perm == "delete" and role == "🗑️ مسؤول حذف": return True
    if perm == "broadcast" and role == "📡 مسؤول إعلانات": return True
    if perm == "settings" and role == "⚙️ مسؤول إعدادات": return True
    return False

# =======================================================
# ⚙️ دوال الحماية للمستخدمين
# =======================================================
def check_vip_status(user_id):
    if is_admin(user_id): return True
    vip_id = get_setting('vip_channel')
    try:
        status = bot.get_chat_member(vip_id, user_id).status
        if status in ['member', 'administrator', 'creator']: return True
        return False
    except: return False

def check_force_sub_status(user_id):
    if is_admin(user_id): return True
    channels = get_setting('force_channels', [])
    for channel in channels:
        try:
            status = bot.get_chat_member(channel["id"], user_id).status
            if status not in ['member', 'administrator', 'creator']:
                return False
        except Exception:
            return False
    return True

def send_force_sub_message(chat_id):
    channels = get_setting('force_channels', [])
    markup = types.InlineKeyboardMarkup(row_width=1)
    for channel in channels:
        markup.add(types.InlineKeyboardButton(f"الانضمام إلى {channel['name']} 📢", url=channel['link']))
    markup.add(types.InlineKeyboardButton("تحقق ✅", callback_data="check_sub"))
    bot.send_message(chat_id,"**عزيزي المستخدم يجب عليك الانضمام إلى قنوات البوت أولاً \nانقر على أزرار الانضمام بالأسفل👇👇👇\nثم عد للبوت وانقر على زر تحقق ✅**", reply_markup=markup, parse_mode="Markdown")

def get_main_menu_markup(chat_id):
    data = load_data()
    series_names = list(data.keys())
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(types.KeyboardButton("🔎 بحث"), types.KeyboardButton("⭐ المفضلة"))
    buttons = [types.KeyboardButton(name) for name in series_names]
    if buttons:
        markup.add(*buttons)
    if is_muted(chat_id): notif_btn = types.KeyboardButton("🔔 تفعيل الإشعارات")
    else: notif_btn = types.KeyboardButton("🔕 كتم الإشعارات")
    markup.row(types.KeyboardButton("📩 طلب مسلسل"), notif_btn)
    if is_admin(chat_id): markup.add(types.KeyboardButton("/admin"))
    return markup

def show_main_menu(chat_id, first_name):
    if chat_id not in user_state: user_state[chat_id] = {}
    markup = get_main_menu_markup(chat_id)
    text = f"مرحباً بك عزيزي ( {first_name} ) 👋\n\n اختر المسلسل للمشاهدة 👇👇"
    bot.send_message(chat_id, text, reply_markup=markup)

# =======================================================
# 🛠️ لوحة تحكم الإدارة (ديناميكية حسب الصلاحيات)
# =======================================================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    uid = message.from_user.id
    if not is_admin(uid): return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    btns = []
    if has_perm(uid, 'add'): btns.extend([types.KeyboardButton("➕ إضافة مسلسل جديد"), types.KeyboardButton("➕ إضافة حلقة لمسلسل")])
    if has_perm(uid, 'delete'): btns.extend([types.KeyboardButton("❌ حذف مسلسل"), types.KeyboardButton("🗑️ حذف حلقة")])
    if has_perm(uid, 'settings'): btns.append(types.KeyboardButton("⚙️ إعدادات القنوات"))
    if has_perm(uid, 'broadcast'): btns.extend([types.KeyboardButton("📊 الإحصائيات"), types.KeyboardButton("📤 إذاعة للكل")])
    
    markup.add(*btns)
    
    # أزرار حصرية للمالك (Super Admin)
    if is_super_admin(uid):
        markup.add(types.KeyboardButton("👥 إدارة المشرفين"), types.KeyboardButton("🧹 تصفير المستخدمين"))
        
    markup.add(types.KeyboardButton("🔙 وضع المستخدم"))
    bot.reply_to(message, "🛠️ **لوحة التحكم الشاملة:**", reply_markup=markup)

@bot.message_handler(commands=['msg'])
def admin_reply_to_user(message):
    if not is_admin(message.from_user.id): return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3: return bot.reply_to(message, "⚠️ خطأ.\nمثال: `/msg 12345 مرحبا`")
        target_id, reply_text = int(parts[1]), parts[2]
        bot.send_message(target_id, f"🔔 **رسالة من الإدارة:**\n\n{reply_text}")
        bot.reply_to(message, "✅ **تم الإرسال!**")
    except Exception as e: bot.reply_to(message, f"❌ فشل: {e}")

# --- إدارة المشرفين الفرعيين (للمالك فقط) ---
@bot.message_handler(func=lambda m: m.text == "👥 إدارة المشرفين" and is_super_admin(m.from_user.id))
def manage_admins_menu(m):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT admin_id, name, role FROM admins")
    admins = c.fetchall()
    conn.close()
    
    text = "👥 **المشرفون الفرعيون:**\n\n"
    for a_id, name, role in admins: text += f"👤 {name} - `{a_id}` - {role}\n"
    if not admins: text += "لا يوجد مشرفين فرعيين.\n"
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("➕ إضافة مشرف"), types.KeyboardButton("❌ طرد مشرف"))
    markup.add(types.KeyboardButton("🔙 رجوع للإدارة"))
    bot.reply_to(m, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "➕ إضافة مشرف" and is_super_admin(m.from_user.id))
def add_admin_step1(m):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "أرسل **آيدي المشرف الجديد**:", reply_markup=markup)
    bot.register_next_step_handler(msg, add_admin_step2)

def add_admin_step2(m):
    if m.text == "❌ إلغاء": return manage_admins_menu(m)
    
    if m.text != "🔙 عودة للصلاحيات":
        try:
            new_admin_id = int(m.text.strip())
            admin_steps[m.chat.id] = {'new_admin': new_admin_id}
        except:
            bot.reply_to(m, "⚠️ آيدي غير صالح.")
            return manage_admins_menu(m)
            
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "أرسل **اسم المشرف** (مثلاً: محمد، عبدالله):", reply_markup=markup)
    bot.register_next_step_handler(msg, add_admin_step_role)

def add_admin_step_role(m):
    if m.text == "❌ إلغاء": return manage_admins_menu(m)
    
    if m.text != "🔙 عودة للصلاحيات":
        admin_steps[m.chat.id]['admin_name'] = m.text.strip()
        
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("👑 مشرف عام", "📤 مسؤول نشر")
    markup.add("🗑️ مسؤول حذف", "📡 مسؤول إعلانات")
    markup.add("⚙️ مسؤول إعدادات", "❌ إلغاء")
    msg = bot.reply_to(m, "اختر **رتبة المشرف** (الصلاحية):", reply_markup=markup)
    bot.register_next_step_handler(msg, process_role_selection)

def process_role_selection(m):
    if m.text == "❌ إلغاء": return manage_admins_menu(m)
    role = m.text
    valid_roles = {
        "👑 مشرف عام": "يملك **جميع الصلاحيات** *باستثناء* إدارة المشرفين وتصفير البوت.",
        "📤 مسؤول نشر": "يمكنه **فقط** إضافة مسلسلات وحلقات جديدة.",
        "🗑️ مسؤول حذف": "يمكنه **فقط** حذف المسلسلات والحلقات.",
        "📡 مسؤول إعلانات": "يمكنه **فقط** عرض الإحصائيات وإرسال رسائل إذاعة.",
        "⚙️ مسؤول إعدادات": "يمكنه **فقط** تعديل قنوات الـ VIP والاشتراك."
    }
    if role not in valid_roles: return manage_admins_menu(m)
    
    admin_steps[m.chat.id]['selected_role'] = role
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("✅ تأكيد الصلاحية"), types.KeyboardButton("🔙 عودة للصلاحيات"))
    markup.add(types.KeyboardButton("❌ إلغاء"))
    
    details = valid_roles[role]
    text = f"🛡️ **تفاصيل رتبة [{role}]:**\n\n🔹 {details}\n\nهل أنت متأكد؟"
    msg = bot.reply_to(m, text, parse_mode="Markdown", reply_markup=markup)
    bot.register_next_step_handler(msg, add_admin_save_final)

def add_admin_save_final(m):
    if m.text == "❌ إلغاء": return manage_admins_menu(m)
    if m.text == "🔙 عودة للصلاحيات": return add_admin_step_role(m)
        
    if m.text == "✅ تأكيد الصلاحية":
        new_admin_id = admin_steps[m.chat.id]['new_admin']
        name = admin_steps[m.chat.id]['admin_name']
        role = admin_steps[m.chat.id]['selected_role']
        try:
            conn = db_connect()
            c = conn.cursor()
            if uses_postgres():
                c.execute(
                    """INSERT INTO admins (admin_id, name, role) VALUES (%s, %s, %s)
                       ON CONFLICT (admin_id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role""",
                    (new_admin_id, name, role),
                )
            else:
                c.execute(
                    "INSERT OR REPLACE INTO admins (admin_id, name, role) VALUES (?, ?, ?)",
                    (new_admin_id, name, role),
                )
            conn.commit()
            conn.close()
            bot.reply_to(m, f"✅ تم إضافة المشرف ({name}) بنجاح برتبة: {role}")
        except: 
            bot.reply_to(m, "⚠️ حدث خطأ أثناء الحفظ.")
        manage_admins_menu(m)

@bot.message_handler(func=lambda m: m.text == "❌ طرد مشرف" and is_super_admin(m.from_user.id))
def rem_admin_step1(m):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT admin_id, name FROM admins")
    admins = c.fetchall()
    conn.close()
    if not admins: return bot.reply_to(m, "القائمة فارغة.")
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # 🟢 إنشاء زر يجمع بين الاسم والآيدي
    markup.add(*[types.KeyboardButton(f"{a[1]} | {a[0]}") for a in admins])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "اختر المشرف لطرده:", reply_markup=markup)
    bot.register_next_step_handler(msg, rem_admin_save)

def rem_admin_save(m):
    if m.text == "❌ إلغاء": return manage_admins_menu(m)
    try:
        # 🟢 استخراج الآيدي من الزر (الذي يكون بصيغة "الاسم | الآيدي")
        del_id = int(m.text.split("|")[-1].strip())
        conn = db_connect()
        c = conn.cursor()
        c.execute(_q("DELETE FROM admins WHERE admin_id=?"), (del_id,))
        conn.commit()
        conn.close()
        bot.reply_to(m, "✅ تم طرد المشرف بنجاح.")
    except Exception as e: 
        bot.reply_to(m, "⚠️ حدث خطأ في الطرد.")
    manage_admins_menu(m)

# --- 1. الإحصائيات والإذاعة ---
@bot.message_handler(func=lambda m: m.text == "📊 الإحصائيات" and has_perm(m.from_user.id, 'broadcast'))
def stats(m):
    data = load_data()
    s_count = len(data)
    ep_count = sum(len(season) for s in data.values() for season in s.get("seasons", {}).values())
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    user_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_muted=1")
    muted_count = c.fetchone()[0]
    conn.close()
    text = (f"📊 **التقرير:**\n👥 المستخدمين: `{user_count}`\n🔕 الكتم: `{muted_count}`\n🎬 المسلسلات: `{s_count}`\n💿 الحلقات: `{ep_count}`")
    bot.reply_to(m, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "📤 إذاعة للكل" and has_perm(m.from_user.id, 'broadcast'))
def broadcast_step(m):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "📝 **أرسل الرسالة:**", reply_markup=markup)
    bot.register_next_step_handler(msg, process_broadcast)

def process_broadcast(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT user_id, is_muted FROM users")
    users = c.fetchall()
    conn.close()
    if not users: return bot.reply_to(message, "⚠️ لا يوجد مستخدمين.")
    bot.reply_to(message, f"🚀 **جاري الإرسال في الخلفية!**\nالعدد: `{len(users)}` مستخدم.", parse_mode="Markdown")
    def send_broadcast_thread():
        success = 0
        blocked = 0
        skipped = 0
        counters_lock = threading.Lock()
        q = queue.Queue(maxsize=5000)

        def worker():
            nonlocal success, blocked, skipped
            while True:
                item = q.get()
                if item is None:
                    q.task_done()
                    break
                uid, muted = item
                try:
                    if muted == 1:
                        with counters_lock:
                            skipped += 1
                    else:
                        bot.copy_message(uid, message.chat.id, message.message_id)
                        with counters_lock:
                            success += 1
                        time.sleep(BROADCAST_DELAY)
                except Exception:
                    with counters_lock:
                        blocked += 1
                finally:
                    q.task_done()

        workers = []
        for _ in range(BROADCAST_WORKERS):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            workers.append(t)

        for user_row in users:
            q.put(user_row)

        for _ in range(BROADCAST_WORKERS):
            q.put(None)

        q.join()
        logging.info(
            "Broadcast done: total=%s success=%s blocked=%s skipped=%s",
            len(users), success, blocked, skipped
        )
        bot.send_message(
            message.chat.id,
            f"✅ **اكتملت الإذاعة!**\n📤 وصل: `{success}`\n🚫 فشل: `{blocked}`\n🔕 تخطّي (مكتوم): `{skipped}`",
            parse_mode="Markdown"
        )
    threading.Thread(target=send_broadcast_thread, daemon=True).start()
    admin_panel(message)

# --- زر تصفير المستخدمين (للمالك فقط) ---
@bot.message_handler(func=lambda m: m.text == "🧹 تصفير المستخدمين" and is_super_admin(m.from_user.id))
def wipe_users_step1(m):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("نعم، متأكد 🧹"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "⚠️ **تحذير خطير:**\nسيتم مسح جميع آيديات المستخدمين! استخدم هذا فقط عند نقل التوكن لبوت جديد.\nهل أنت متأكد؟", reply_markup=markup)
    bot.register_next_step_handler(msg, wipe_users_step2)

def wipe_users_step2(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "نعم، متأكد 🧹":
        try:
            conn = db_connect()
            c = conn.cursor()
            c.execute("DELETE FROM users")
            conn.commit()
            conn.close()
            bot.reply_to(message, "✅ **تم تصفير قاعدة المستخدمين بنجاح!**\nالمسلسلات والإعدادات ما زالت آمنة.")
        except Exception as e: bot.reply_to(message, f"❌ خطأ: {e}")
    admin_panel(message)

# --- إعدادات القنوات الديناميكية ---
@bot.message_handler(func=lambda m: m.text == "⚙️ إعدادات القنوات" and has_perm(m.from_user.id, 'settings'))
def channel_settings_menu(m):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("👑 قناة البوابة (VIP)"), types.KeyboardButton("📢 قنوات الاشتراك"))
    markup.add(types.KeyboardButton("🔙 رجوع للإدارة"))
    bot.reply_to(m, "⚙️ **إعدادات القنوات:**\nاختر ما تريد تعديله:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "🔙 رجوع للإدارة" and (is_super_admin(m.from_user.id) or has_perm(m.from_user.id, "settings")))
def back_to_admin_panel_from_submenus(m):
    admin_panel(m)

# 1. إعدادات VIP
@bot.message_handler(func=lambda m: m.text == "👑 قناة البوابة (VIP)" and has_perm(m.from_user.id, 'settings'))
def edit_vip_step1(m):
    current = get_setting('vip_channel')
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, f"الآيدي الحالي: `{current}`\n\nأرسل **آيدي القناة الجديدة** (يبدأ بـ -100):", parse_mode="Markdown", reply_markup=markup)
    bot.register_next_step_handler(msg, edit_vip_step2)

def edit_vip_step2(message):
    if message.text == "❌ إلغاء": return channel_settings_menu(message)
    try:
        new_id = int(message.text.strip())
        set_setting('vip_channel', new_id)
        bot.reply_to(message, f"✅ تم حفظ آيدي الـ VIP الجديد: `{new_id}`", parse_mode="Markdown")
    except: bot.reply_to(message, "⚠️ آيدي غير صالح.")
    channel_settings_menu(message)

# 2. إعدادات الاشتراك الإجباري
@bot.message_handler(func=lambda m: m.text == "📢 قنوات الاشتراك" and has_perm(m.from_user.id, 'settings'))
def force_channels_menu(m):
    channels = get_setting('force_channels', [])
    text = "📢 **قنوات الاشتراك الحالية:**\n\n"
    for idx, ch in enumerate(channels): text += f"{idx+1}. {ch['name']} | `{ch['id']}`\n"
    if not channels: text += "لا يوجد قنوات مضافة."
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("➕ إضافة قناة"), types.KeyboardButton("❌ إزالة قناة"))
    markup.add(types.KeyboardButton("🔙 رجوع للإعدادات"))  # 🟢 زر الرجوع المعدل
    bot.reply_to(m, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "🔙 رجوع للإعدادات" and has_perm(m.from_user.id, 'settings'))
def back_to_settings_menu_handler(m): channel_settings_menu(m)

@bot.message_handler(func=lambda m: m.text == "➕ إضافة قناة" and has_perm(m.from_user.id, 'settings'))
def add_force_ch_step1(m):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "أرسل **اسم القناة**:", reply_markup=markup)
    bot.register_next_step_handler(msg, add_force_ch_step2)

def add_force_ch_step2(m):
    if m.text == "❌ إلغاء": return force_channels_menu(m)
    admin_steps[m.chat.id] = {'ch_name': m.text}
    msg = bot.reply_to(m, "أرسل **رابط القناة** (يبدأ بـ https):")
    bot.register_next_step_handler(msg, add_force_ch_step3)

def add_force_ch_step3(m):
    if m.text == "❌ إلغاء": return force_channels_menu(m)
    admin_steps[m.chat.id]['ch_link'] = m.text
    msg = bot.reply_to(m, "أرسل **آيدي القناة** (يبدأ بـ -100):")
    bot.register_next_step_handler(msg, add_force_ch_save)

def add_force_ch_save(m):
    if m.text == "❌ إلغاء": return force_channels_menu(m)
    link = (admin_steps.get(m.chat.id) or {}).get("ch_link", "").strip()
    if not link.startswith("https://"):
        bot.reply_to(m, "⚠️ رابط القناة يجب أن يبدأ بـ https://")
        return force_channels_menu(m)
    try:
        ch_id = int(m.text)
        new_channel = {"name": admin_steps[m.chat.id]['ch_name'], "link": link, "id": ch_id}
        channels = get_setting('force_channels', [])
        channels.append(new_channel)
        set_setting('force_channels', channels)
        bot.reply_to(m, "✅ تم إضافة القناة بنجاح!")
    except: bot.reply_to(m, "⚠️ خطأ في إدخال الآيدي.")
    force_channels_menu(m)

@bot.message_handler(func=lambda m: m.text == "❌ إزالة قناة" and has_perm(m.from_user.id, 'settings'))
def rem_force_ch_step1(m):
    channels = get_setting('force_channels', [])
    if not channels: return bot.reply_to(m, "القائمة فارغة.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(*[types.KeyboardButton(ch['name']) for ch in channels])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "اختر القناة لحذفها:", reply_markup=markup)
    bot.register_next_step_handler(msg, rem_force_ch_save)

def rem_force_ch_save(m):
    if m.text == "❌ إلغاء": return force_channels_menu(m)
    channels = get_setting('force_channels', [])
    new_channels = [ch for ch in channels if ch['name'] != m.text]
    set_setting('force_channels', new_channels)
    bot.reply_to(m, "✅ تم حذف القناة.")
    force_channels_menu(m)

# --- إدارة المسلسلات (إضافة وحذف) ---
@bot.message_handler(func=lambda m: m.text == "➕ إضافة مسلسل جديد" and has_perm(m.from_user.id, 'add'))
def add_series_step1(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, "📝 أرسل **اسم المسلسل**:", reply_markup=markup)
    bot.register_next_step_handler(msg, add_series_step2_channel)

def add_series_step2_channel(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    series_name = message.text.strip()
    data = load_data()
    if series_name in data:
        bot.reply_to(message, "⚠️ موجود مسبقاً!")
        return admin_panel(message)
    admin_steps[message.chat.id] = {'series_name': series_name}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, f"📺 أرسل **آيدي القناة** (-100xxxx):", reply_markup=markup)
    bot.register_next_step_handler(msg, process_save_series)

def process_save_series(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return add_series_step1(message)
    try:
        channel_id = int(message.text.strip())
        series_name = admin_steps[message.chat.id]['series_name']
        data = load_data()
        data[series_name] = {"channel_id": channel_id, "seasons": {}}
        save_data(data)
        bot.reply_to(message, f"✅ تم إضافة: **{series_name}**")
        admin_panel(message)
    except:
        bot.reply_to(message, "⚠️ آيدي خاطئ.")
        bot.register_next_step_handler(message, process_save_series)

@bot.message_handler(func=lambda m: m.text == "❌ حذف مسلسل" and has_perm(m.from_user.id, 'delete'))
def delete_series(message):
    data = load_data()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(*[types.KeyboardButton(n) for n in data.keys()])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, "🗑️ **اختر للحذف:**", reply_markup=markup)
    bot.register_next_step_handler(msg, process_delete)

def process_delete(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text in load_data():
        data = load_data()
        del data[message.text]
        save_data(data)
        bot.reply_to(message, "✅ تم الحذف.")
    admin_panel(message)

# --- حذف حلقة مخصصة ---
@bot.message_handler(func=lambda m: m.text == "🗑️ حذف حلقة" and has_perm(m.from_user.id, 'delete'))
def del_ep_step1(m):
    data = load_data()
    if not data: return bot.reply_to(m, "⚠️ لا توجد مسلسلات.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(*[types.KeyboardButton(n) for n in data.keys()])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "📂 **اختر المسلسل:**", reply_markup=markup)
    bot.register_next_step_handler(msg, del_ep_step2)

def del_ep_step2(m):
    if m.text == "❌ إلغاء": return admin_panel(m)
    series = m.text
    data = load_data()
    if series not in data: return bot.reply_to(m, "⚠️ غير موجود.")
    admin_steps[m.chat.id] = {'del_series': series}
    seasons = list(data[series]['seasons'].keys())
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(*[types.KeyboardButton(s) for s in seasons])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "📅 **اختر الموسم:**", reply_markup=markup)
    bot.register_next_step_handler(msg, del_ep_step3)

def del_ep_step3(m):
    if m.text == "❌ إلغاء": return admin_panel(m)
    season = m.text
    series = admin_steps[m.chat.id]['del_series']
    data = load_data()
    if season not in data[series]['seasons']: return bot.reply_to(m, "⚠️ غير موجود.")
    admin_steps[m.chat.id]['del_season'] = season
    eps = list(data[series]['seasons'][season].keys())
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=4)
    markup.add(*[types.KeyboardButton(e) for e in eps])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(m, "🔢 **اختر الحلقة المراد حذفها نهائياً:**", reply_markup=markup)
    bot.register_next_step_handler(msg, del_ep_final)

def del_ep_final(m):
    if m.text == "❌ إلغاء": return admin_panel(m)
    ep = m.text
    series = admin_steps[m.chat.id]['del_series']
    season = admin_steps[m.chat.id]['del_season']
    data = load_data()
    try:
        del data[series]['seasons'][season][ep]
        save_data(data)
        bot.reply_to(m, f"✅ تم حذف الحلقة {ep} من {season} بنجاح!")
    except: bot.reply_to(m, "⚠️ خطأ في الحذف.")
    admin_panel(m)

# --- 4. إضافة حلقة ---
@bot.message_handler(func=lambda m: m.text == "➕ إضافة حلقة لمسلسل" and has_perm(m.from_user.id, 'add'))
def add_ep_step1(message):
    data = load_data()
    if not data: return bot.reply_to(message, "⚠️ لا توجد مسلسلات.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(*[types.KeyboardButton(n) for n in data.keys()])
    markup.add(types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, "📂 **اختر المسلسل:**", reply_markup=markup)
    bot.register_next_step_handler(msg, add_ep_step2_season)

def add_ep_step2_season(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    series_name = message.text
    data = load_data()
    if series_name not in data: return bot.reply_to(message, "⚠️ غير موجود.")
    admin_steps[message.chat.id] = {'series': series_name}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    seasons = [f"الموسم {i}" for i in range(1, 6)]
    markup.add(*[types.KeyboardButton(s) for s in seasons])
    markup.add(types.KeyboardButton("✏️ موسم آخر"))
    markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, f"📅 **اختر الموسم:**", reply_markup=markup)
    bot.register_next_step_handler(msg, add_ep_step3_ep)

def add_ep_step3_ep(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return add_ep_step1(message)
    season_name = message.text
    if message.text == "✏️ موسم آخر":
        msg = bot.reply_to(message, "📝 اكتب اسم الموسم:", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, add_ep_step3_ep)
        return
    admin_steps[message.chat.id]['season'] = season_name
    show_episode_selection(message)

def show_episode_selection(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=5)
    markup.add(*[types.KeyboardButton(str(i)) for i in range(1, 51)])
    markup.add(types.KeyboardButton("✏️ يدوي"))
    markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, f"🔢 **اختر رقم الحلقة:**", reply_markup=markup)
    bot.register_next_step_handler(msg, process_episode_check)

def process_episode_check(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع":
        msg = message
        msg.text = admin_steps[message.chat.id]['series']
        return add_ep_step2_season(msg)
    if message.text == "✏️ يدوي":
        msg = bot.reply_to(message, "📝 اكتب الرقم:", reply_markup=types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_episode_check)
        return
    ep_num = message.text.strip()
    admin_steps[message.chat.id]['ep'] = ep_num
    series = admin_steps[message.chat.id]['series']
    season = admin_steps[message.chat.id]['season']
    data = load_data()
    if ep_num in data.get(series, {}).get("seasons", {}).get(season, {}):
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row(types.KeyboardButton("➕ إضافة جودة جديدة"), types.KeyboardButton("✏️ تعديل جودة واحدة"))
        markup.add(types.KeyboardButton("🔄 استبدال الكل (تحديث شامل)"))
        markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        msg = bot.reply_to(message, f"⚠️ **تنبيه:** الحلقة {ep_num} موجودة!\nماذا تريد أن تفعل؟", reply_markup=markup)
        bot.register_next_step_handler(msg, process_overwrite_decision)
    else: ask_upload_mode(message)

def process_overwrite_decision(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return show_episode_selection(message)
    if "إضافة جودة" in message.text: ask_upload_mode(message)
    elif "استبدال الكل" in message.text:
        series = admin_steps[message.chat.id]['series']
        season = admin_steps[message.chat.id]['season']
        ep = admin_steps[message.chat.id]['ep']
        data = load_data()
        data[series]["seasons"][season][ep] = {} 
        save_data(data)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        markup.add(*[types.KeyboardButton(q) for q in QUALITY_ORDER])
        markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        msg = bot.reply_to(message, "🗑️ تم الحذف.\n📉 **اختر أقل جودة (البداية):**", reply_markup=markup)
        bot.register_next_step_handler(msg, batch_step1_start)
    elif "تعديل جودة واحدة" in message.text:
        series = admin_steps[message.chat.id]['series']
        season = admin_steps[message.chat.id]['season']
        ep = admin_steps[message.chat.id]['ep']
        data = load_data()
        existing_quals = sort_existing_qualities(data[series]["seasons"][season][ep].keys())
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        markup.add(*[types.KeyboardButton(q) for q in existing_quals])
        markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        msg = bot.reply_to(message, "📝 **اختر الجودة لتعديلها:**", reply_markup=markup)
        bot.register_next_step_handler(msg, edit_single_quality_step1)

def edit_single_quality_step1(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return show_episode_selection(message)
    qual = message.text
    admin_steps[message.chat.id]['edit_qual'] = qual
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, f"🔗 جودة {qual} - **أرسل الآيدي الجديد:**", reply_markup=markup)
    bot.register_next_step_handler(msg, edit_single_quality_save)

def edit_single_quality_save(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع":
        msg = message
        msg.text = "تعديل جودة واحدة"
        return process_overwrite_decision(msg)
    try:
        new_msg_id = int(message.text.strip())
        series = admin_steps[message.chat.id]['series']
        season = admin_steps[message.chat.id]['season']
        ep = admin_steps[message.chat.id]['ep']
        qual = admin_steps[message.chat.id]['edit_qual']
        admin_steps[message.chat.id]['pending_edit_quality'] = {
            "series": series,
            "season": season,
            "ep": ep,
            "qual": qual,
            "msg_id": new_msg_id,
        }
        review = (
            f"📋 **مراجعة قبل الحفظ — تعديل جودة**\n"
            f"المسلسل: {series}\nالموسم: {season}\nالحلقة: {ep}\nالجودة: {qual}\nآيدي جديد: `{new_msg_id}`"
        )
        msg = bot.reply_to(
            message,
            review,
            parse_mode="Markdown",
            reply_markup=admin_save_confirm_markup(),
        )
        bot.register_next_step_handler(msg, confirm_edit_quality_save)
    except Exception:
        bot.reply_to(message, "⚠️ رقم خطأ.")
        admin_panel(message)


def confirm_edit_quality_save(message):
    cid = message.chat.id
    p = admin_steps.get(cid, {}).get("pending_edit_quality")
    if not p:
        return admin_panel(message)
    if message.text == "❌ إلغاء":
        admin_steps[cid].pop("pending_edit_quality", None)
        return admin_panel(message)
    if message.text == "↩️ إعادة الإدخال":
        admin_steps[cid].pop("pending_edit_quality", None)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        qual = admin_steps[cid]["edit_qual"]
        msg = bot.reply_to(
            message,
            f"🔗 جودة {qual} - **أرسل الآيدي الجديد مجددًا:**",
            reply_markup=markup,
        )
        bot.register_next_step_handler(msg, edit_single_quality_save)
        return
    if message.text != "✅ تأكيد الحفظ":
        return bot.reply_to(message, "⚠️ استخدم الأزرار أدناه.")
    series, season, ep = p["series"], p["season"], p["ep"]
    qual, new_msg_id = p["qual"], p["msg_id"]
    data = load_data()
    if season not in data[series]["seasons"]:
        data[series]["seasons"][season] = {}
    if ep not in data[series]["seasons"][season]:
        data[series]["seasons"][season][ep] = {}
    data[series]["seasons"][season][ep][qual] = new_msg_id
    save_data(data)
    admin_steps[cid].pop("pending_edit_quality", None)
    ask_next_action(message)

def ask_upload_mode(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("🚀 عدة جودات (تلقائي)"), types.KeyboardButton("☝️ جودة واحدة (يدوي)"))
    markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, "⚙️ **طريقة الإضافة:**", reply_markup=markup)
    bot.register_next_step_handler(msg, process_mode_selection)

def process_mode_selection(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return show_episode_selection(message)
    if "تلقائي" in message.text:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        markup.add(*[types.KeyboardButton(q) for q in QUALITY_ORDER])
        markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        msg = bot.reply_to(message, "📉 **اختر أقل جودة (البداية):**", reply_markup=markup)
        bot.register_next_step_handler(msg, batch_step1_start)
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        markup.add(*[types.KeyboardButton(q) for q in QUALITY_ORDER])
        markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        msg = bot.reply_to(message, f"📺 **اختر الجودة:**", reply_markup=markup)
        bot.register_next_step_handler(msg, add_ep_step5_save_manual)

def batch_step1_start(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return ask_upload_mode(message)
    start_qual = message.text
    if start_qual not in QUALITY_ORDER: return bot.reply_to(message, "⚠️ جودة غير معروفة.")
    admin_steps[message.chat.id]['start_qual'] = start_qual
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(*[types.KeyboardButton(q) for q in QUALITY_ORDER])
    markup.row(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(message, f"📈 **البداية: {start_qual}**\nاختر **أعلى جودة (النهاية):**", reply_markup=markup)
    bot.register_next_step_handler(msg, batch_step2_end)

def batch_step2_end(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return ask_upload_mode(message)
    end_qual = message.text
    if end_qual not in QUALITY_ORDER: return bot.reply_to(message, "⚠️ جودة غير معروفة.")
    start_qual = admin_steps[message.chat.id]['start_qual']
    start_idx = QUALITY_ORDER.index(start_qual)
    end_idx = QUALITY_ORDER.index(end_qual)
    if end_idx < start_idx: return bot.reply_to(message, "⚠️ خطأ: النهاية أقل من البداية!")
    admin_steps[message.chat.id]['end_qual'] = end_qual
    count = end_idx - start_idx + 1
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    msg = bot.reply_to(
        message,
        f"✅ سيتم إضافة **{count}** جودات.\n🔢 **أرسل الآيدي للجودة ({start_qual}) فقط:**\n\n"
        "ℹ️ التلقائي يفترض آيديات متسلسلة (+1 لكل جودة). إن لم يكن الأمر كذلك استخدم «☝️ جودة واحدة (يدوي)».",
        reply_markup=markup,
    )
    bot.register_next_step_handler(msg, batch_step3_process)

def batch_step3_process(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return ask_upload_mode(message)
    try:
        start_id = int(message.text.strip())
        series = admin_steps[message.chat.id]['series']
        season = admin_steps[message.chat.id]['season']
        ep = admin_steps[message.chat.id]['ep']
        start_q = admin_steps[message.chat.id]['start_qual']
        end_q = admin_steps[message.chat.id]['end_qual']
        start_idx = QUALITY_ORDER.index(start_q)
        end_idx = QUALITY_ORDER.index(end_q)
        mapping = {}
        current_id = start_id
        for i in range(start_idx, end_idx + 1):
            q_name = QUALITY_ORDER[i]
            mapping[q_name] = current_id
            current_id += 1
        admin_steps[message.chat.id]["pending_batch"] = {
            "series": series,
            "season": season,
            "ep": ep,
            "mapping": mapping,
        }
        lines = "\n".join(f"• **{q}**: `{mid}`" for q, mid in mapping.items())
        review = (
            f"📋 **مراجعة قبل الحفظ — {len(mapping)} جودات**\n"
            f"المسلسل: {series} | الموسم: {season} | الحلقة: {ep}\n\n{lines}"
        )
        msg = bot.reply_to(
            message,
            review,
            parse_mode="Markdown",
            reply_markup=admin_save_confirm_markup(),
        )
        bot.register_next_step_handler(msg, confirm_batch_episode_save)
    except ValueError:
        bot.reply_to(message, "⚠️ الرقم غير صحيح.")


def confirm_batch_episode_save(message):
    cid = message.chat.id
    p = admin_steps.get(cid, {}).get("pending_batch")
    if not p:
        return admin_panel(message)
    if message.text == "❌ إلغاء":
        admin_steps[cid].pop("pending_batch", None)
        return admin_panel(message)
    if message.text == "↩️ إعادة الإدخال":
        admin_steps[cid].pop("pending_batch", None)
        start_qual = admin_steps[cid]["start_qual"]
        end_qual = admin_steps[cid]["end_qual"]
        start_idx = QUALITY_ORDER.index(start_qual)
        end_idx = QUALITY_ORDER.index(end_qual)
        count = end_idx - start_idx + 1
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        msg = bot.reply_to(
            message,
            f"🔢 **أرسل مجددًا الآيدي للجودة ({start_qual}) فقط:**\n(سيتم إضافة **{count}** جودات)",
            reply_markup=markup,
        )
        bot.register_next_step_handler(msg, batch_step3_process)
        return
    if message.text != "✅ تأكيد الحفظ":
        return bot.reply_to(message, "⚠️ استخدم الأزرار أدناه.")
    series, season, ep = p["series"], p["season"], p["ep"]
    mapping = p["mapping"]
    data = load_data()
    if season not in data[series]["seasons"]:
        data[series]["seasons"][season] = {}
    if ep not in data[series]["seasons"][season]:
        data[series]["seasons"][season][ep] = {}
    for q_name, mid in mapping.items():
        data[series]["seasons"][season][ep][q_name] = mid
    save_data(data)
    admin_steps[cid].pop("pending_batch", None)
    ask_next_action(message)

def add_ep_step5_save_manual(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return ask_upload_mode(message)
    qual = message.text
    admin_steps[message.chat.id]['qual'] = qual
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
    info = f"{admin_steps[message.chat.id]['series']} | {admin_steps[message.chat.id]['season']} | ح{admin_steps[message.chat.id]['ep']} | {qual}"
    msg = bot.reply_to(message, f"🔗 {info}\n\n**أرسل الآيدي:**", reply_markup=markup)
    bot.register_next_step_handler(msg, save_ep_final_manual)

def save_ep_final_manual(message):
    if message.text == "❌ إلغاء": return admin_panel(message)
    if message.text == "🔙 رجوع": return ask_upload_mode(message)
    try:
        msg_id = int(message.text.strip())
        series = admin_steps[message.chat.id]['series']
        season = admin_steps[message.chat.id]['season']
        ep = admin_steps[message.chat.id]['ep']
        qual = admin_steps[message.chat.id]['qual']
        admin_steps[message.chat.id]["pending_manual"] = {
            "series": series,
            "season": season,
            "ep": ep,
            "qual": qual,
            "msg_id": msg_id,
        }
        review = (
            f"📋 **مراجعة قبل الحفظ — جودة واحدة**\n"
            f"المسلسل: {series}\nالموسم: {season}\nالحلقة: {ep}\nالجودة: {qual}\nآيدي الرسالة: `{msg_id}`"
        )
        msg = bot.reply_to(
            message,
            review,
            parse_mode="Markdown",
            reply_markup=admin_save_confirm_markup(),
        )
        bot.register_next_step_handler(msg, confirm_manual_episode_save)
    except Exception:
        bot.reply_to(message, "⚠️ خطأ.")
        admin_panel(message)


def confirm_manual_episode_save(message):
    cid = message.chat.id
    p = admin_steps.get(cid, {}).get("pending_manual")
    if not p:
        return admin_panel(message)
    if message.text == "❌ إلغاء":
        admin_steps[cid].pop("pending_manual", None)
        return admin_panel(message)
    if message.text == "↩️ إعادة الإدخال":
        admin_steps[cid].pop("pending_manual", None)
        qual = admin_steps[cid]["qual"]
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(types.KeyboardButton("🔙 رجوع"), types.KeyboardButton("❌ إلغاء"))
        info = f"{admin_steps[cid]['series']} | {admin_steps[cid]['season']} | ح{admin_steps[cid]['ep']} | {qual}"
        msg = bot.reply_to(
            message,
            f"🔗 {info}\n\n**أرسل الآيدي مجددًا:**",
            reply_markup=markup,
        )
        bot.register_next_step_handler(msg, save_ep_final_manual)
        return
    if message.text != "✅ تأكيد الحفظ":
        return bot.reply_to(message, "⚠️ استخدم الأزرار أدناه.")
    series, season, ep = p["series"], p["season"], p["ep"]
    qual, msg_id = p["qual"], p["msg_id"]
    data = load_data()
    if season not in data[series]["seasons"]:
        data[series]["seasons"][season] = {}
    if ep not in data[series]["seasons"][season]:
        data[series]["seasons"][season][ep] = {}
    data[series]["seasons"][season][ep][qual] = msg_id
    save_data(data)
    admin_steps[cid].pop("pending_manual", None)
    ask_next_action(message)

def ask_next_action(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton("🔄 نفس المسلسل والموسم (حلقة جديدة)"))
    markup.add(types.KeyboardButton("📂 مسلسل آخر"))
    markup.add(types.KeyboardButton("🏠 القائمة الرئيسية"))
    msg = bot.reply_to(message, "✅ **تم الحفظ! ماذا الآن؟**", reply_markup=markup)
    bot.register_next_step_handler(msg, process_next_action)

def process_next_action(message):
    text = message.text
    if text == "🏠 القائمة الرئيسية": admin_panel(message)
    elif text == "📂 مسلسل آخر": add_ep_step1(message)
    elif "نفس المسلسل" in text:
        series = admin_steps[message.chat.id]['series']
        season = admin_steps[message.chat.id]['season']
        bot.send_message(message.chat.id, f"👌 **إضافة لـ: {series} - {season}**")
        show_episode_selection(message)

# =======================================================
# 📱 وضع المستخدم (محدث)
# =======================================================
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if not check_vip_status(user_id):
        bot.send_message(message.chat.id, "🛠️ **البوت قيد الصيانة حالياً... يرجى المحاولة لاحقاً.**")
        return
    save_user(user_id)
    if not check_force_sub_status(user_id):
        send_force_sub_message(message.chat.id)
        return
    show_main_menu(message.chat.id, message.from_user.first_name)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub_btn(call):
    user_id = call.from_user.id
    if not check_vip_status(user_id):
        bot.answer_callback_query(call.id, "🛠️ البوت قيد الصيانة.", show_alert=True)
        return
    if check_force_sub_status(user_id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_main_menu(call.message.chat.id, call.from_user.first_name)
    else:
        bot.answer_callback_query(call.id, "❌ يجب عليك الانضمام إلى جميع القنوات أولاً!", show_alert=True)

@bot.message_handler(func=lambda m: m.text in ("🔔 تفعيل الإشعارات", "🔕 كتم الإشعارات"))
def toggle_notifications(m):
    if not check_vip_status(m.from_user.id):
        return
    if not check_force_sub_status(m.from_user.id):
        send_force_sub_message(m.chat.id)
        return
    save_user(m.from_user.id)
    state = toggle_mute_status(m.from_user.id)
    note = "ستصلك رسائل الإذاعة من البوت." if state == "unmuted" else "لن تصلك رسائل الإذاعة حتى تفعّل الإشعارات."
    bot.reply_to(m, f"✅ **{note}**", parse_mode="Markdown", reply_markup=get_main_menu_markup(m.chat.id))

# 1. طلب مسلسل
@bot.message_handler(func=lambda m: m.text == "📩 طلب مسلسل")
def request_series_handler(m):
    if not check_vip_status(m.from_user.id): return
    if not check_force_sub_status(m.from_user.id):
        send_force_sub_message(m.chat.id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🔙 إلغاء"))
    msg = bot.reply_to(m, "ملاحظة ⚠️ نوفر فقط المسلسلات التركية والمتوفرة على موقعنا الرسمي www.ofira.site  \n📝 اكتب اسم المسلسل الذي تريد طلبه:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_request)


@bot.message_handler(func=lambda m: m.text == "🔎 بحث")
def search_series_handler(m):
    if not check_vip_status(m.from_user.id): return
    if not check_force_sub_status(m.from_user.id):
        send_force_sub_message(m.chat.id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🔙 إلغاء"))
    msg = bot.reply_to(m, "🔎 اكتب اسم المسلسل أو جزء منه:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_series_search)


def process_series_search(message):
    if message.text == "🔙 إلغاء":
        return show_main_menu(message.chat.id, message.from_user.first_name)
    query = (message.text or "").strip()
    if not query:
        return bot.reply_to(message, "⚠️ اكتب كلمة بحث صحيحة.")
    results = search_series_names(query)
    if not results:
        return bot.reply_to(message, "❌ لا توجد نتائج. جرّب كلمة أخرى أو استخدم زر طلب مسلسل.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(*[types.KeyboardButton(n) for n in results])
    markup.add(types.KeyboardButton("الرجوع للقائمة الرئيسية 🔙"))
    bot.reply_to(message, f"✅ نتائج البحث عن: **{query}**", parse_mode="Markdown", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "⭐ المفضلة")
def favorites_list_handler(m):
    if not check_vip_status(m.from_user.id): return
    if not check_force_sub_status(m.from_user.id):
        send_force_sub_message(m.chat.id)
        return
    all_series = load_data()
    favs = [s for s in get_user_favorites(m.from_user.id) if s in all_series]
    if not favs:
        return bot.reply_to(m, "⭐ لا توجد مسلسلات في المفضلة بعد.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(*[types.KeyboardButton(s) for s in favs])
    markup.add(types.KeyboardButton("الرجوع للقائمة الرئيسية 🔙"))
    bot.reply_to(m, "⭐ **مفضلتك:** اختر مسلسلًا.", parse_mode="Markdown", reply_markup=markup)

def process_request(message):
    if message.text == "🔙 إلغاء": return show_main_menu(message.chat.id, message.from_user.first_name)
    query = message.text
    data = load_data()
    results = [name for name in data.keys() if query in name]
    if results:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(*[types.KeyboardButton(n) for n in results])
        markup.add(types.KeyboardButton("الرجوع للقائمة الرئيسية 🔙"))
        bot.reply_to(message, "✅ **هذا المسلسل متوفر لدينا:**", parse_mode="Markdown", reply_markup=markup)
    else:
        bot.reply_to(message, "⚠️ المسلسل غير متوفر حالياً.\n📨 **تم إرسال طلبك للإدارة!**")
        admin_text = (f"📨 **طلب جديد!**\n👤: {message.from_user.first_name}\n🆔: `{message.from_user.id}`\n📺: {query}\n\nللرد: `/msg {message.from_user.id} الرد`")
        for admin in ADMIN_IDS:
            try: bot.send_message(admin, admin_text, parse_mode="Markdown")
            except: pass
        show_main_menu(message.chat.id, message.from_user.first_name)

# 2. اختيار المسلسل
@bot.message_handler(func=lambda m: m.text in load_data())
def select_series(message):
    if not check_vip_status(message.from_user.id): return
    if not check_force_sub_status(message.from_user.id):
        send_force_sub_message(message.chat.id)
        return
    series_name = message.text 
    data = load_data()
    if series_name not in data: return 
    user_state[message.chat.id] = {'series': series_name}
    seasons_data = data[series_name]["seasons"]
    seasons = sort_season_keys(seasons_data.keys())
    if not seasons: return bot.reply_to(message, "⚠️ لا توجد مواسم.")
    favorite_btn = "💔 إزالة من المفضلة" if is_favorite(message.from_user.id, series_name) else "⭐ إضافة للمفضلة"
    ratings_count, ratings_avg = get_series_rating_stats(series_name)
    my_rating = get_user_series_rating(message.from_user.id, series_name)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row(types.KeyboardButton(favorite_btn), types.KeyboardButton("⭐ تقييم المسلسل"))
    markup.add(*[types.KeyboardButton(s) for s in seasons])
    markup.add(types.KeyboardButton("الرجوع للقائمة الرئيسية 🔙"))
    rating_text = "لا يوجد تقييم بعد" if ratings_count == 0 else f"{ratings_avg:.1f}/5 ({ratings_count})"
    my_rating_text = "-" if my_rating is None else str(my_rating)
    bot.reply_to(
        message,
        f"📺 **{series_name}**\n⭐ التقييم العام: **{rating_text}**\n🧑 تقييمك: **{my_rating_text}**\nاختر الموسم 👇",
        parse_mode="Markdown",
        reply_markup=markup
    )


@bot.message_handler(func=lambda m: m.text in ("⭐ إضافة للمفضلة", "💔 إزالة من المفضلة"))
def toggle_favorite_handler(m):
    if m.chat.id not in user_state or "series" not in user_state[m.chat.id]:
        return show_main_menu(m.chat.id, m.from_user.first_name)
    series_name = user_state[m.chat.id]["series"]
    if m.text == "⭐ إضافة للمفضلة":
        ok = add_favorite(m.from_user.id, series_name)
        msg = "✅ تمت إضافة المسلسل إلى المفضلة." if ok else "⚠️ تعذر إضافة المسلسل."
    else:
        ok = remove_favorite(m.from_user.id, series_name)
        msg = "✅ تمت إزالة المسلسل من المفضلة." if ok else "⚠️ تعذر إزالة المسلسل."
    bot.reply_to(m, msg)
    m.text = series_name
    select_series(m)


@bot.message_handler(func=lambda m: m.text == "⭐ تقييم المسلسل")
def rate_series_start(m):
    if m.chat.id not in user_state or "series" not in user_state[m.chat.id]:
        return show_main_menu(m.chat.id, m.from_user.first_name)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(
        types.KeyboardButton("1 ⭐"),
        types.KeyboardButton("2 ⭐"),
        types.KeyboardButton("3 ⭐"),
        types.KeyboardButton("4 ⭐"),
        types.KeyboardButton("5 ⭐"),
    )
    markup.add(types.KeyboardButton("🔙 إلغاء"))
    msg = bot.reply_to(m, "⭐ اختر تقييمك من 1 إلى 5:", reply_markup=markup)
    bot.register_next_step_handler(msg, save_series_rating_step)


def save_series_rating_step(message):
    if message.text == "🔙 إلغاء":
        if message.chat.id in user_state and "series" in user_state[message.chat.id]:
            message.text = user_state[message.chat.id]["series"]
            return select_series(message)
        return show_main_menu(message.chat.id, message.from_user.first_name)
    parts = (message.text or "").split()
    if not parts or not parts[0].isdigit():
        return bot.reply_to(message, "⚠️ اختر تقييمًا صحيحًا من الأزرار.")
    value = int(parts[0])
    if value < 1 or value > 5:
        return bot.reply_to(message, "⚠️ التقييم يجب أن يكون من 1 إلى 5.")
    if message.chat.id not in user_state or "series" not in user_state[message.chat.id]:
        return show_main_menu(message.chat.id, message.from_user.first_name)
    series_name = user_state[message.chat.id]["series"]
    ok = set_series_rating(message.from_user.id, series_name, value)
    if ok:
        bot.reply_to(message, f"✅ تم حفظ تقييمك: {value}/5")
    else:
        bot.reply_to(message, "⚠️ تعذر حفظ التقييم.")
    message.text = series_name
    select_series(message)

# 3. اختيار الموسم (أي اسم موسم مسجّل في البيانات، وليس فقط «الموسم …»)
@bot.message_handler(func=is_user_selecting_season)
def select_season(message):
    if not check_vip_status(message.from_user.id): return
    if not check_force_sub_status(message.from_user.id):
        send_force_sub_message(message.chat.id)
        return
    if message.chat.id not in user_state: return show_main_menu(message.chat.id, message.from_user.first_name)
    season_name = message.text
    user_state[message.chat.id]['season'] = season_name
    series = user_state[message.chat.id]['series']
    data = load_data()
    episodes_data = data.get(series, {}).get("seasons", {}).get(season_name)
    if not episodes_data:
        return bot.reply_to(message, "⚠️ لا توجد حلقات.")
    episodes = sort_episode_keys(episodes_data.keys())
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=4)
    markup.add(*[types.KeyboardButton(f"الحلقة {ep}") for ep in episodes])
    markup.add(types.KeyboardButton("رجوع للمواسم 🔙"))
    bot.reply_to(message, f"📺 {series} - {season_name}\n**اختر الحلقة:**", reply_markup=markup)

# 4. اختيار الحلقة
@bot.message_handler(func=lambda m: m.text and m.text.startswith("الحلقة"))
def select_ep(message):
    if not check_vip_status(message.from_user.id): return
    if not check_force_sub_status(message.from_user.id):
        send_force_sub_message(message.chat.id)
        return
    if message.chat.id not in user_state: return show_main_menu(message.chat.id, message.from_user.first_name)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(message, "⚠️ اختر حلقة من الأزرار.")
    ep_num = parts[1].strip()
    user_state[message.chat.id]['ep'] = ep_num
    series = user_state[message.chat.id]['series']
    season = user_state[message.chat.id]['season']
    data = load_data()
    available_qualities = data.get(series, {}).get("seasons", {}).get(season, {}).get(ep_num, {})
    if not available_qualities: return bot.reply_to(message, "⚠️ جاري اضافة هذه الحلقة.")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    buttons = []
    for q_key in QUALITY_ORDER:
        if q_key in available_qualities:
            btn_text = QUALITY_LABELS.get(q_key, q_key)
            buttons.append(types.KeyboardButton(btn_text))
    markup.add(*buttons)
    markup.add(types.KeyboardButton("رجوع للحلقات 🔙"))
    bot.reply_to(message, f"✅ الحلقة {ep_num}\n**الجودات المتوفرة:**", reply_markup=markup)

# 5. الإرسال
@bot.message_handler(func=lambda m: m.text.endswith(("☁️", "📱", "🎥", "📺", "ᴴᴰ", "ᶠᴴᴰ", "ᵁᴴᴰ")))
def send_video(message):
    if not check_vip_status(message.from_user.id): return
    if not check_force_sub_status(message.from_user.id):
        send_force_sub_message(message.chat.id)
        return
    if message.chat.id not in user_state: return bot.reply_to(message, "⚠️ ابدأ من القائمة.")
    try:
        s_data = user_state[message.chat.id]
        series = s_data['series']
        season = s_data['season']
        ep = s_data['ep']
        quality_key = None
        for key, label in QUALITY_LABELS.items():
            if message.text == label:
                quality_key = key
                break
        if not quality_key: return
        data = load_data()
        target_channel_id = data[series]["channel_id"]
        ep_data = data[series]["seasons"][season][ep]
        if quality_key not in ep_data: return bot.reply_to(message, "⚠️ غير متوفرة.")
        msg_id = ep_data[quality_key]
        caption_text = (f"<b>🎬 {series}</b>\n<b>💿 الحلقة {ep}</b>\n<b>📺 الجودة: {message.text}</b>\n\n<blockquote>جميع الحقوق محفوظة لموقع فيديو اوفيرا</blockquote>\nwww.ofira.site")
        bot.copy_message(message.chat.id, target_channel_id, msg_id, caption=caption_text, parse_mode="HTML")
    except Exception:
        bot.reply_to(message, "⚠️ تعذر إرسال الحلقة. حاول لاحقاً أو راجع الإدارة.")

# --- أزرار الرجوع ---
@bot.message_handler(func=lambda m: m.text == "الرجوع للقائمة الرئيسية 🔙")
def back_home(m): show_main_menu(m.chat.id, m.from_user.first_name)

@bot.message_handler(func=lambda m: m.text == "رجوع للمواسم 🔙")
def back_seasons(m):
    if m.chat.id in user_state and 'series' in user_state[m.chat.id]:
        msg = m
        msg.text = user_state[m.chat.id]['series']
        select_series(msg)
    else: show_main_menu(m.chat.id, m.from_user.first_name)

@bot.message_handler(func=lambda m: m.text == "رجوع للحلقات 🔙")
def back_eps(m):
    if m.chat.id in user_state and 'season' in user_state[m.chat.id]:
        msg = m
        msg.text = user_state[m.chat.id]['season']
        select_season(msg)
    else: show_main_menu(m.chat.id, m.from_user.first_name)

@bot.message_handler(func=lambda m: m.text == "🔙 وضع المستخدم")
def exit_admin(message):
    show_main_menu(message.chat.id, message.from_user.first_name)

if __name__ == "__main__":
    bot.infinity_polling()