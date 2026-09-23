# ============================================================
# Aurora AI Bot - النسخة النهائية المستقرة والسحابية
# Python 3.12+ Compatible | Vision + Auto Translate + PostgreSQL/SQLite
# ============================================================

import os
import re
import time
import base64
import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from flask import Flask, jsonify

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ============================================================
# الإعدادات
# ============================================================

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_TOKEN", "")
).strip()

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
).strip()

# رابط قاعدة البيانات السحابية (مثال: PostgreSQL على Supabase أو Neon)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen/qwen3.8-27b")
LINK_MODEL = os.getenv("LINK_MODEL", "groq/compound-mini")
BASE_URL = "https://api.groq.com/openai/v1"
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "600"))
DB_FILE = "bot_data.sqlite3"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN غير موجود")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY غير موجود")

ai_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=BASE_URL)

# ============================================================
# الثوابت
# ============================================================

INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

STATUS_EXISTS = "exists"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"

STATUS_TEXT = {
    STATUS_EXISTS: "✅ الحساب موجود",
    STATUS_UNAVAILABLE: "❌ الحساب غير متوفر",
    STATUS_UNKNOWN: "⚠️ تعذر التحقق حالياً",
}

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._]{1,30}$")
URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')
last_ai_request = {}

flask_app = Flask(__name__)

@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    return jsonify({"status": "ok", "bot": "Aurora AI Running"}), 200

# ============================================================
# إدارة قاعدة البيانات (يدعم PostgreSQL إذا وجد وإلا يتراجع لـ SQLite)
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def db_connection():
    if DATABASE_URL.startswith("postgres"):
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

def init_database():
    conn = db_connection()
    cursor = conn.cursor()
    
    if DATABASE_URL.startswith("postgres"):
        query = """
        CREATE TABLE IF NOT EXISTS watched_users (
            chat_id BIGINT NOT NULL,
            username VARCHAR(100) NOT NULL,
            last_status VARCHAR(50),
            created_at VARCHAR(100) NOT NULL,
            updated_at VARCHAR(100) NOT NULL,
            PRIMARY KEY (chat_id, username)
        );
        """
    else:
        query = """
        CREATE TABLE IF NOT EXISTS watched_users (
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            last_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, username)
        );
        """
    cursor.execute(query)
    conn.commit()
    conn.close()

def add_watched_user(chat_id: int, username: str, status: str):
    conn = db_connection()
    cursor = conn.cursor()
    now = utc_now()
    
    if DATABASE_URL.startswith("postgres"):
        cursor.execute(
            """
            INSERT INTO watched_users (chat_id, username, last_status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, username) 
            DO UPDATE SET last_status = EXCLUDED.last_status, updated_at = EXCLUDED.updated_at;
            """,
            (chat_id, username, status, now, now),
        )
    else:
        cursor.execute(
            """
            INSERT INTO watched_users VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE SET last_status=excluded.last_status, updated_at=excluded.updated_at;
            """,
            (chat_id, username, status, now, now),
        )
    conn.commit()
    conn.close()

def remove_watched_user(chat_id: int, username: str) -> bool:
    conn = db_connection()
    cursor = conn.cursor()
    placeholder = "%s" if DATABASE_URL.startswith("postgres") else "?"
    cursor.execute(
        f"DELETE FROM watched_users WHERE chat_id = {placeholder} AND username = {placeholder}",
        (chat_id, username),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted

def get_chat_users(chat_id: int):
    conn = db_connection()
    cursor = conn.cursor()
    placeholder = "%s" if DATABASE_URL.startswith("postgres") else "?"
    cursor.execute(
        f"SELECT username, last_status FROM watched_users WHERE chat_id = {placeholder} ORDER BY username",
        (chat_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_all_watched_users():
    conn = db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, username, last_status FROM watched_users ORDER BY username")
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_user_status(chat_id: int, username: str, status: str):
    conn = db_connection()
    cursor = conn.cursor()
    ph = "%s" if DATABASE_URL.startswith("postgres") else "?"
    cursor.execute(
        f"UPDATE watched_users SET last_status = {ph}, updated_at = {ph} WHERE chat_id = {ph} AND username = {ph}",
        (status, utc_now(), chat_id, username),
    )
    conn.commit()
    conn.close()

# ============================================================
# أدوات مساعدة
# ============================================================

def normalize_username(username: str):
    username = username.strip().replace("@", "").lower()
    if not USERNAME_PATTERN.fullmatch(username):
        return None
    return username

def ai_rate_limited(chat_id: int) -> bool:
    now = time.time()
    last = last_ai_request.get(chat_id, 0)
    if now - last < 3:
        return True
    last_ai_request[chat_id] = now
    return False

# ============================================================
# Instagram Checker
# ============================================================

async def check_instagram_username(username: str) -> str:
    username = normalize_username(username)
    if not username:
        return STATUS_UNKNOWN

    profile_url = f"https://www.instagram.com/{quote(username)}/"

    try:
        async with httpx.AsyncClient(
            headers=INSTAGRAM_HEADERS,
            follow_redirects=True,
            timeout=15,
        ) as client:
            response = await client.get(profile_url)
            body = response.text.lower()

            if response.status_code == 404:
                return STATUS_UNAVAILABLE

            if response.status_code in (401, 403, 429, 500, 502, 503, 504):
                return STATUS_UNKNOWN

            unavailable = [
                "sorry, this page isn't available",
                "the link you followed may be broken",
                "page isn't available",
                "user not found",
            ]
            if any(p in body for p in unavailable):
                return STATUS_UNAVAILABLE

            escaped = re.escape(username)
            if response.status_code == 200 and re.search(rf"instagram\.com/{escaped}/", body, re.IGNORECASE):
                return STATUS_EXISTS

            return STATUS_UNKNOWN

    except Exception as e:
        print(f"Instagram error: {e}")
        return STATUS_UNKNOWN

# ============================================================
# AI Functions
# ============================================================

DEFAULT_SYSTEM = (
    "أنت Aurora AI، مساعد ذكي مفيد. "
    "أجب بالعربية بوضوح، واستخدم اللهجة العراقية إذا كان المستخدم يستخدمها. "
    "كن دقيقاً ومباشراً ولا تخترع معلومات."
)

async def ask_ai(prompt: str, system_prompt: str | None = None) -> str:
    if not system_prompt:
        system_prompt = DEFAULT_SYSTEM
    try:
        response = await ai_client.chat.completions.create(
            model=CHAT_MODEL,
            temperature=0.3,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        return content.strip() if content else "⚠️ ما وصلني رد. جرب مرة ثانية."
    except Exception as e:
        print("AI ERROR:", e)
        return f"❌ خطأ: {str(e)[:100]}"

async def ask_ai_vision(image_bytes: bytes, user_prompt: str = "") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = user_prompt if user_prompt else (
        "اكتشف النص المكتوب داخل هذه الصورة وترجمه بدقة إلى اللغة العربية. "
        "إذا لم تحتوِ الصورة على نص، قم بتحليل محتواها ووصف العناصر والمشهد بالتفصيل."
    )
    try:
        response = await ai_client.chat.completions.create(
            model=VISION_MODEL,
            temperature=0.4,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        return content.strip() if content else "⚠️ ما كدرت أحلل الصورة."
    except Exception as e:
        print("VISION ERROR:", e)
        return f"❌ خطأ في تحليل الصورة: {str(e)[:100]}"

async def fetch_url_text(url: str) -> str:
    """جلب المحتوى النصي من الرابط لتقليله وتحليله"""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            res = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if res.status_code == 200:
                text = re.sub(r'<[^>]+>', ' ', res.text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:4000]
    except Exception as e:
        print(f"Error fetching URL: {e}")
    return ""

async def ask_ai_link(url: str) -> str:
    """جلب النص ثم تحليله بواسطة الذكاء الاصطناعي"""
    page_text = await fetch_url_text(url)
    
    if not page_text:
        prompt = f"حلل هذا الرابط وأعطني نبذة عنه بناءً على عنوانه: {url}"
    else:
        prompt = f"إليك المحتوى المستخرج من الرابط ({url}):\n\n{page_text}\n\nيرجى قراءته وتلخيصه بالعربية."

    return await ask_ai(prompt)

# ============================================================
# Handlers
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *أهلاً بك في Aurora AI*\n\n"
        "أرسل أي رسالة، صورة (للترجمة والتحليل التلقائي)، أو رابط وأنا أرد عليك.\n\n"
        "📋 *الأوامر:*\n"
        "/ai سؤال — اسأل الذكاء الاصطناعي\n"
        "/analyze نص — تحليل نص\n"
        "/translate اللغة نص — ترجمة\n"
        "/check username — فحص حساب إنستغرام\n"
        "/watch username — حفظ ومراقبة تلقائية\n"
        "/unwatch username — إلغاء المراقبة\n"
        "/list — الحسابات المحفوظة"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    url_match = URL_PATTERN.search(text)
    if url_match:
        url = url_match.group(0)
        await update.message.reply_text(f"🔎 جاري تحليل محتوى الرابط...\n{url}")
        answer = await ask_ai_link(url)
        await update.message.reply_text(answer)
        return

    answer = await ask_ai(text)
    await update.message.reply_text(answer)

async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الصور: الترجمة والتحليل التلقائي بذكاء"""
    if not update.message or not update.message.photo:
        return

    chat_id = update.effective_chat.id
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        
        caption = update.message.caption or ""
        
        # إذا لم يرفق المستخدم أي نص مع الصورة، نعتمد تعليمات الترجمة والتحليل التلقائي
        if not caption.strip():
            caption = (
                "اقرأ أي نص مكتوب أو موجود في الصورة ثم ترجمه بدقة إلى اللغة العربية. "
                "إذا لم يكن هناك أي نص في الصورة، قم بوصف وتحليل مكوناتها بالتفصيل."
            )

        answer = await ask_ai_vision(image_bytes, caption)
        await update.message.reply_text(answer)
    except Exception as e:
        print("PHOTO ERROR:", e)
        await update.message.reply_text("❌ ما كدرت أعالج الصورة.")

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/ai سؤالك")
        return
    prompt = " ".join(context.args)
    chat_id = update.effective_chat.id
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    answer = await ask_ai(prompt)
    await update.message.reply_text(answer)

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/analyze نص")
        return
    text = " ".join(context.args)
    chat_id = update.effective_chat.id
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    prompt = f"حلل النص التالي بالتفصيل:\n\n{text}\n\nأعطني: الفكرة الرئيسية، النبرة، النقاط المهمة، المخاطر، اقتراح."
    answer = await ask_ai(prompt)
    await update.message.reply_text(answer)

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/translate English نص")
        return
    if len(context.args) >= 2:
        target = context.args[0]
        text = " ".join(context.args[1:])
    else:
        target = "العربية"
        text = context.args[0]
    chat_id = update.effective_chat.id
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    prompt = f"ترجم النص التالي إلى {target}:\n\n{text}\n\nأرسل الترجمة فقط."
    answer = await ask_ai(prompt, "أنت مترجم محترف.")
    await update.message.reply_text(answer)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/check username")
        return
    username = normalize_username(context.args[0])
    if not username:
        await update.message.reply_text("❌ اليوزر غير صحيح.")
        return
    await update.message.reply_text(f"🔎 جاري فحص @{username}...")
    status = await check_instagram_username(username)
    await update.message.reply_text(f"@{username}\n{STATUS_TEXT[status]}")

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/watch username")
        return
    username = normalize_username(context.args[0])
    if not username:
        await update.message.reply_text("❌ اليوزر غير صحيح.")
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🔎 أفحص @{username}...")
    status = await check_instagram_username(username)
    add_watched_user(chat_id, username, status)
    msg = (
        f"💾 تم حفظ @{username} للمراقبة.\n"
        f"الحالة الحالية: {STATUS_TEXT[status]}\n\n"
        f"⏱️ سأفحصه كل {MONITOR_INTERVAL // 60} دقائق."
    )
    await update.message.reply_text(msg)

async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/unwatch username")
        return
    username = normalize_username(context.args[0])
    if not username:
        await update.message.reply_text("❌ اليوزر غير صحيح.")
        return
    deleted = remove_watched_user(update.effective_chat.id, username)
    if deleted:
        await update.message.reply_text(f"🗑️ تم حذف @{username} من المراقبة.")
    else:
        await update.message.reply_text(f"⚠️ @{username} غير موجود.")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_chat_users(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 لا توجد حسابات محفوظة.")
        return
    lines = ["📋 *الحسابات المراقبة:*\n"]
    for row in rows:
        u = row["username"]
        s = row["last_status"] or STATUS_UNKNOWN
        lines.append(f"• @{u} — {STATUS_TEXT.get(s, STATUS_UNKNOWN)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ============================================================
# Monitor Loop
# ============================================================

async def monitor_loop(application: Application):
    print("Monitor started.")
    await asyncio.sleep(30)
    while True:
        try:
            rows = get_all_watched_users()
            if not rows:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
            grouped: dict = {}
            for row in rows:
                grouped.setdefault(row["username"], []).append(row)
            for username, watchers in grouped.items():
                try:
                    current = await check_instagram_username(username)
                except Exception as e:
                    print(f"Check failed: {e}")
                    continue
                if current == STATUS_UNKNOWN:
                    continue
                for watcher in watchers:
                    chat_id = watcher["chat_id"]
                    previous = watcher["last_status"]
                    update_user_status(chat_id, username, current)
                    if previous in (STATUS_UNAVAILABLE, None) and current == STATUS_EXISTS:
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=f"🔔 *خبر حلو!*\n\n@{username} صار متوفراً!\n🔗 https://instagram.com/{username}",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            print(f"Notify failed: {e}")
                await asyncio.sleep(2)
        except Exception as e:
            print(f"Monitor error: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)

async def post_init(application: Application):
    asyncio.create_task(monitor_loop(application))

# ============================================================
# Flask Server
# ============================================================

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)

# ============================================================
# Main Execution
# ============================================================

def main():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    init_database()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("translate", translate_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))
    application.add_handler(CommandHandler("list", list_command))

    application.add_handler(MessageHandler(filters.PHOTO, photo_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ai_message))

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("Flask started.")
    print("Bot started...")
    
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
