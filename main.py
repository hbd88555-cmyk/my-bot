# ============================================================
# Aurora AI Bot - النسخة الكاملة
# Python 3.14 Compatible
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
from flask import Flask

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
    os.getenv("TELEGRAM_BOT_TOKEN", "")
    or os.getenv("TELEGRAM_TOKEN", "")
).strip()

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY", "")
    or os.getenv("OPENAI_API_KEY", "")
).strip()

CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")
VISION_MODEL = os.getenv("VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
last_ai_request = {}

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "Aurora AI is running!"


# ============================================================
# قاعدة البيانات
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watched_users (
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            last_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, username)
        )
        """
    )
    conn.commit()
    conn.close()


def add_watched_user(chat_id: int, username: str, status: str):
    conn = db_connection()
    existing = conn.execute(
        "SELECT 1 FROM watched_users WHERE chat_id = ? AND username = ?",
        (chat_id, username),
    ).fetchone()
    now = utc_now()
    if existing:
        conn.execute(
            "UPDATE watched_users SET last_status = ?, updated_at = ? "
            "WHERE chat_id = ? AND username = ?",
            (status, now, chat_id, username),
        )
    else:
        conn.execute(
            "INSERT INTO watched_users VALUES (?, ?, ?, ?, ?)",
            (chat_id, username, status, now, now),
        )
    conn.commit()
    conn.close()


def remove_watched_user(chat_id: int, username: str) -> bool:
    conn = db_connection()
    cursor = conn.execute(
        "DELETE FROM watched_users WHERE chat_id = ? AND username = ?",
        (chat_id, username),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def get_chat_users(chat_id: int):
    conn = db_connection()
    rows = conn.execute(
        "SELECT username, last_status FROM watched_users "
        "WHERE chat_id = ? ORDER BY username",
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def get_all_watched_users():
    conn = db_connection()
    rows = conn.execute(
        "SELECT chat_id, username, last_status FROM watched_users "
        "ORDER BY username"
    ).fetchall()
    conn.close()
    return rows


def update_user_status(chat_id: int, username: str, status: str):
    conn = db_connection()
    conn.execute(
        "UPDATE watched_users SET last_status = ?, updated_at = ? "
        "WHERE chat_id = ? AND username = ?",
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
            timeout=20,
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
                "this account is unavailable",
            ]
            if any(p in body for p in unavailable):
                return STATUS_UNAVAILABLE

            escaped = re.escape(username)
            patterns = [
                rf'"username"\s*:\s*"{escaped}"',
                rf"instagram\.com/{escaped}/",
                rf"@{escaped}",
            ]

            if response.status_code == 200:
                if any(re.search(p, body, re.IGNORECASE) for p in patterns):
                    return STATUS_EXISTS
                if "login" in body and "instagram" in body:
                    return STATUS_UNKNOWN

            api_url = "https://www.instagram.com/api/v1/users/web_profile_info/"
            api_response = await client.get(api_url, params={"username": username})

            if api_response.status_code == 200:
                try:
                    data = api_response.json()
                    if data.get("data", {}).get("user"):
                        return STATUS_EXISTS
                    return STATUS_UNAVAILABLE
                except Exception:
                    return STATUS_UNKNOWN

            if api_response.status_code == 404:
                return STATUS_UNAVAILABLE

            return STATUS_UNKNOWN

    except Exception as e:
        print(f"Instagram check error: {e}")
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
        if not content:
            return "⚠️ ما وصلني رد. جرب مرة ثانية."
        return content.strip()
    except Exception as e:
        print("AI ERROR:", e)
        return f"❌ خطأ: {str(e)[:100]}"


async def ask_ai_vision(image_bytes: bytes, user_prompt: str = "") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = user_prompt if user_prompt else "اوصف هذه الصورة بالتفصيل بالعربية."

    try:
        response = await ai_client.chat.completions.create(
            model=VISION_MODEL,
            temperature=0.5,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        if not content:
            return "⚠️ ما كدرت أحلل الصورة."
        return content.strip()
    except Exception as e:
        print("VISION ERROR:", e)
        return f"❌ خطأ في تحليل الصورة: {str(e)[:100]}"


# ============================================================
# Handlers
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *أهلاً بك في Aurora AI*\n\n"
        "أرسل أي رسالة أو صورة وأنا أرد عليك.\n\n"
        "📋 *الأوامر:*\n"
        "/ai سؤال — اسأل الذكاء الاصطناعي\n"
        "/analyze نص — تحليل نص\n"
        "/translate اللغة نص — ترجمة\n"
        "/check username — فحص حساب إنستغرام\n"
        "/watch username — حفظ ومراقبة تلقائية\n"
        "/unwatch username — إلغاء المراقبة\n"
        "/list — الحسابات المحفوظة\n"
        "/help — المساعدة"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    answer = await ask_ai(update.message.text)
    await update.message.reply_text(answer)


async def photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        caption = update.message.caption or ""
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
    prompt = (
        f"حلل النص التالي بالتفصيل:\n\n{text}\n\n"
        "أعطني: الفكرة الرئيسية، النبرة، النقاط المهمة، المخاطر، اقتراح."
    )
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
        f"⏱️ سأفحصه كل {MONITOR_INTERVAL // 60} دقائق، "
        f"وسأخبرك إذا صار متوفراً."
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
        await update.message.reply_text(f"⚠️ @{username} غير موجود في قائمة المراقبة.")


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
# المراقبة التلقائية
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

            print(f"Checking {len(grouped)} watched accounts...")

            for username, watchers in grouped.items():
                try:
                    current = await check_instagram_username(username)
                except Exception as e:
                    print(f"Check failed for @{username}: {e}")
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
                                text=(
                                    f"🔔 *خبر حلو!*\n\n"
                                    f"حساب @{username} صار متوفراً على إنستغرام!\n\n"
                                    f"🔗 https://instagram.com/{username}"
                                ),
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
# Flask
# ============================================================

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


# ============================================================
# main
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
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, ai_message)
    )

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
