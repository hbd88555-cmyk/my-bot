# main.py
import os
import re
import time
import sqlite3
import asyncio
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

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "")
    or os.getenv("TELEGRAM_TOKEN", "")
).strip()

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY", "")
    or os.getenv("OPENAI_API_KEY", "")
).strip()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-oss-120b")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN غير موجود")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY غير موجود")

ai_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url=OPENAI_BASE_URL,
)

DB_FILE = "bot_data.sqlite3"

INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "X-IG-App-ID": "936619743392459",
}

STATUS_EXISTS = "exists"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"

STATUS_TEXT = {
    STATUS_EXISTS: "✅ الحساب موجود",
    STATUS_UNAVAILABLE: "❌ الحساب غير متوفر",
    STATUS_UNKNOWN: "⚠️ تعذر التحقق حالياً، حاول لاحقاً",
}

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._]{1,30}$")

last_ai_request = {}

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running!"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def db_connection():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    connection = db_connection()
    connection.execute(
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
    connection.commit()
    connection.close()


def normalize_username(username: str):
    username = username.strip().replace("@", "").lower()
    if not USERNAME_PATTERN.fullmatch(username):
        return None
    return username


def add_watched_user(chat_id: int, username: str, status: str | None):
    connection = db_connection()
    existing = connection.execute(
        "SELECT last_status FROM watched_users WHERE chat_id = ? AND username = ?",
        (chat_id, username),
    ).fetchone()
    now = utc_now()
    if existing:
        if status in (STATUS_EXISTS, STATUS_UNAVAILABLE):
            connection.execute(
                "UPDATE watched_users SET last_status = ?, updated_at = ? WHERE chat_id = ? AND username = ?",
                (status, now, chat_id, username),
            )
    else:
        connection.execute(
            "INSERT INTO watched_users (chat_id, username, last_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, username, status, now, now),
        )
    connection.commit()
    connection.close()


def remove_watched_user(chat_id: int, username: str):
    connection = db_connection()
    cursor = connection.execute(
        "DELETE FROM watched_users WHERE chat_id = ? AND username = ?",
        (chat_id, username),
    )
    connection.commit()
    deleted = cursor.rowcount > 0
    connection.close()
    return deleted


def get_chat_users(chat_id: int):
    connection = db_connection()
    rows = connection.execute(
        "SELECT username, last_status FROM watched_users WHERE chat_id = ? ORDER BY username",
        (chat_id,),
    ).fetchall()
    connection.close()
    return rows


def get_all_watched_users():
    connection = db_connection()
    rows = connection.execute(
        "SELECT chat_id, username, last_status FROM watched_users ORDER BY username"
    ).fetchall()
    connection.close()
    return rows


def update_user_status(chat_id: int, username: str, status: str):
    connection = db_connection()
    connection.execute(
        "UPDATE watched_users SET last_status = ?, updated_at = ? WHERE chat_id = ? AND username = ?",
        (status, utc_now(), chat_id, username),
    )
    connection.commit()
    connection.close()


async def check_instagram_username(username: str):
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

            unavailable_phrases = [
                "sorry, this page isn't available",
                "the link you followed may be broken",
                "page isn't available",
                "user not found",
                "this account is unavailable",
            ]

            if any(phrase in body for phrase in unavailable_phrases):
                return STATUS_UNAVAILABLE

            escaped_username = re.escape(username)

            profile_patterns = [
                rf'"username"\s*:\s*"{escaped_username}"',
                rf'"username"\s*:\s*"{escaped_username.lower()}"',
                rf"instagram\.com/{escaped_username}/",
                rf"@{escaped_username}",
            ]

            if response.status_code == 200:
                if any(re.search(pattern, body, re.IGNORECASE) for pattern in profile_patterns):
                    return STATUS_EXISTS
                if "login" in body and "instagram" in body:
                    return STATUS_UNKNOWN

            api_url = "https://www.instagram.com/api/v1/users/web_profile_info/"
            api_response = await client.get(api_url, params={"username": username})

            if api_response.status_code == 200:
                try:
                    data = api_response.json()
                    user = data.get("data", {}).get("user")
                    if user:
                        return STATUS_EXISTS
                    return STATUS_UNAVAILABLE
                except Exception:
                    return STATUS_UNKNOWN

            if api_response.status_code == 404:
                return STATUS_UNAVAILABLE

            return STATUS_UNKNOWN

    except (httpx.TimeoutException, httpx.NetworkError):
        return STATUS_UNKNOWN
    except Exception:
        return STATUS_UNKNOWN


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🤖 أهلاً بك\n\n"
        "أرسل أي رسالة حتى أجاوبك بالذكاء الاصطناعي.\n\n"
        "الأوامر:\n"
        "/ai سؤالك\n"
        "/analyze النص للتحليل\n"
        "/translate اللغة النص\n"
        "/check username فحص حساب إنستغرام\n"
        "/watch username حفظ ومراقبة حساب\n"
        "/unwatch username إلغاء المراقبة\n"
        "/list عرض الحسابات المحفوظة\n"
        "/help عرض المساعدة"
    )
    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def ask_ai(prompt: str, system_prompt: str | None = None):
    if not system_prompt:
        system_prompt = (
            "أنت مساعد ذكاء اصطناعي مفيد. "
            "أجب باللغة العربية الواضحة، واستخدم اللهجة العراقية إذا كان سؤال المستخدم باللهجة العراقية. "
            "كن دقيقاً ومباشراً ولا تخترع معلومات."
        )

    response = await ai_client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    
    content = response.choices[0].message.content if response.choices else None
    if not content:
        return "⚠️ ما وصلني رد. جرب مرة ثانية."
    return content.strip()


def ai_rate_limited(chat_id: int):
    current_time = time.time()
    last_time = last_ai_request.get(chat_id, 0)
    if current_time - last_time < 3:
        return True
    last_ai_request[chat_id] = current_time
    return False


async def ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني قبل إرسال طلب جديد.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        answer = await ask_ai(update.message.text)
        await update.message.reply_text(answer)
    except Exception as error:
        print("AI ERROR:", error)
        await update.message.reply_text("❌ حدث خطأ أثناء الاتصال بالذكاء الاصطناعي.")


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/ai اكتب سؤالك هنا")
        return

    prompt = " ".join(context.args)
    chat_id = update.effective_chat.id

    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني قبل إرسال طلب جديد.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        answer = await ask_ai(prompt)
        await update.message.reply_text(answer)
    except Exception as error:
        print("AI COMMAND ERROR:", error)
        await update.message.reply_text("❌ حدث خطأ أثناء الاتصال بالذكاء الاصطناعي.")


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/analyze النص الذي تريد تحليله")
        return

    text = " ".join(context.args)
    chat_id = update.effective_chat.id

    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني قبل إرسال طلب جديد.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    prompt = f"""حلل النص التالي بالتفصيل وبشكل منظم:

{text}

أعطني:
1. الفكرة الرئيسية
2. المشاعر أو النبرة
3. النقاط المهمة
4. أي مشاكل أو مخاطر
5. اقتراح عملي مناسب
"""

    try:
        answer = await ask_ai(prompt)
        await update.message.reply_text(answer)
    except Exception as error:
        print("ANALYZE ERROR:", error)
        await update.message.reply_text("❌ حدث خطأ أثناء تحليل النص.")


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "الاستخدام:\n/translate English هذا النص\n\nمثال:\n/translate Arabic Hello, how are you?"
        )
        return

    if len(context.args) >= 2:
        target_language = context.args[0]
        text = " ".join(context.args[1:])
    else:
        target_language = "العربية"
        text = context.args[0]

    chat_id = update.effective_chat.id

    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني قبل إرسال طلب جديد.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    prompt = f"""ترجم النص التالي إلى لغة {target_language}.
حافظ على المعنى والنبرة.
أرسل الترجمة فقط بدون شرح:

{text}
"""

    try:
        answer = await ask_ai(
            prompt,
            system_prompt=(
                "أنت مترجم محترف. ترجم بدقة، وحافظ على المعنى والنبرة، ولا تضف أي شرح غير مطلوب."
            ),
        )
        await update.message.reply_text(answer)
    except Exception as error:
        print("TRANSLATE ERROR:", error)
        await update.message.reply_text("❌ حدث خطأ أثناء الترجمة.")


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
    await update.message.reply_text(f"Instagram: @{username}\n{STATUS_TEXT[status]}")


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/watch username")
        return

    username = normalize_username(context.args[0])
    if not username:
        await update.message.reply_text("❌ اليوزر غير صحيح.")
        return

    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🔎 أفحص @{username} ثم أحفظه للمراقبة...")
    status = await check_instagram_username(username)
    add_watched_user(chat_id, username, status)
    await update.message.reply_text(
        f"💾 تم حفظ @{username} للمراقبة.\n\n"
        f"{STATUS_TEXT[status]}\n\n"
        f"سأرسل لك رسالة عندما يتغير الحساب من غير متوفر إلى موجود."
    )


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
        await update.message.reply_text(f"🗑️ تم إلغاء مراقبة @{username}.")
    else:
        await update.message.reply_text(f"⚠️ @{username} غير موجود ضمن قائمة المراقبة.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_chat_users(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 لا توجد حسابات محفوظة.")
        return

    lines = ["📋 الحسابات المحفوظة:\n"]
    for row in rows:
        username = row["username"]
        status = row["last_status"] or STATUS_UNKNOWN
        lines.append(f"@{username} — {STATUS_TEXT.get(status, STATUS_TEXT[STATUS_UNKNOWN])}")

    await update.message.reply_text("\n".join(lines))


async def arabic_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split()

    if len(parts) < 2:
        await update.message.reply_text(
            "الاستخدام:\n/فحص username\n/حفظ username\n/حذف username"
        )
        return

    command = parts[0].split("@")[0].lower()

    if command in ("/فحص", "/check"):
        await check_command(update, context)
    elif command in ("/حفظ", "/راقب", "/watch"):
        await watch_command(update, context)
    elif command in ("/حذف", "/الغاء", "/unwatch"):
        await unwatch_command(update, context)


async def monitor_instagram_accounts(context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_watched_users()
    if not rows:
        return

    grouped = {}
    for row in rows:
        username = row["username"]
        grouped.setdefault(username, []).append(row)

    for username, watchers in grouped.items():
        current_status = await check_instagram_username(username)
        if current_status == STATUS_UNKNOWN:
            continue

        for watcher in watchers:
            chat_id = watcher["chat_id"]
            previous_status = watcher["last_status"]
            update_user_status(chat_id, username, current_status)

            if previous_status == STATUS_UNAVAILABLE and current_status == STATUS_EXISTS:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔔 الحساب نشط الآن!\n\n@{username}\n✅ الحساب موجود على إنستغرام.",
                    )
                except Exception as error:
                    print("SEND NOTIFICATION ERROR:", error)

        await asyncio.sleep(2)


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


def main():
    init_database()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("analyze", analyze_command))
    application.add_handler(CommandHandler("translate", translate_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("save", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))
    application.add_handler(CommandHandler("list", list_command))

    arabic_commands = re.compile(
        r"^/(?:فحص|حفظ|راقب|حذف|الغاء)(?:@\w+)?(?:\s+.+)?$",
        re.IGNORECASE,
    )

    application.add_handler(
        MessageHandler(filters.Regex(arabic_commands), arabic_aliases)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, ai_message)
    )

    try:
        application.job_queue.run_repeating(
            monitor_instagram_accounts,
            interval=CHECK_INTERVAL_SECONDS,
            first=30,
            name="instagram_monitor",
        )
        print("JobQueue started.")
    except Exception as e:
        print("JobQueue error:", e)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("Flask started on port", os.environ.get("PORT", 10000))

    print("Bot started...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
