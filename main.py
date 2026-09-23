تفضل الكود كامل:

```python
# ============================================================
# Aurora AI Bot - النسخة الشاملة المدموجة
# Python 3.12+ | Railway Ready
# ============================================================

import os
import re
import time
import asyncio
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from flask import Flask, jsonify
from dotenv import load_dotenv
from openai import AsyncOpenAI

try:
    from tavily import AsyncTavilyClient
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = (
    os.getenv("BOT_TOKEN", "")
    or os.getenv("TELEGRAM_TOKEN", "")
    or os.getenv("TELEGRAM_BOT_TOKEN", "")
).strip()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

CHAT_MODEL = "openai/gpt-oss-120b"
WHISPER_MODEL = "whisper-large-v3-turbo"
BASE_URL = "https://api.groq.com/openai/v1"
DB_FILE = "bot_data.sqlite3"
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "600"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY غير موجود")

ai_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=BASE_URL)

search_client = None
if TAVILY_AVAILABLE and TAVILY_API_KEY:
    try:
        search_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
    except Exception as e:
        print(f"Tavily init failed: {e}")

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

user_history = {}
active_users = set()
last_ai_request = {}

SYSTEM_PROMPT = (
    "أنت Aurora AI، مساعد ذكاء اصطناعي عربي متطور وذكي وسريع. "
    "تُجيب بدقة، وتساعد في البرمجة والتحليل والترجمة والأخبار، "
    "وتتحدث بأسلوب ودي واحترافي."
)

flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    return jsonify({"status": "ok", "bot": "Aurora AI Running"}), 200


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


async def search_web(query: str) -> str:
    if not search_client:
        return "⚠️ خدمة البحث غير مفعّلة."
    try:
        response = await search_client.search(
            query=query, search_depth="basic", max_results=3
        )
        results = response.get("results", [])
        if not results:
            return "لم يتم العثور على نتائج."
        out = "📰 **المعلومات:**\n\n"
        for idx, r in enumerate(results, 1):
            out += f"{idx}. [{r['title']}]({r['url']})\n{r['content']}\n\n"
        return out
    except Exception as e:
        print("SEARCH ERROR:", e)
        return "❌ فشل البحث."


async def ask_ai_with_history(chat_id: int, user_prompt: str) -> str:
    if chat_id not in user_history:
        user_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    user_history[chat_id].append({"role": "user", "content": user_prompt})
    if len(user_history[chat_id]) > 10:
        user_history[chat_id] = [user_history[chat_id][0]] + user_history[chat_id][-9:]
    try:
        response = await ai_client.chat.completions.create(
            model=CHAT_MODEL,
            temperature=0.4,
            max_tokens=2048,
            messages=user_history[chat_id],
        )
        content = response.choices[0].message.content if response.choices else "⚠️ لم يتم استلام رد."
        user_history[chat_id].append({"role": "assistant", "content": content})
        return content
    except Exception as e:
        print("AI ERROR:", e)
        return f"❌ خطأ: {str(e)[:150]}"


async def check_instagram_username(username: str) -> str:
    username = normalize_username(username)
    if not username:
        return STATUS_UNKNOWN
    profile_url = f"https://www.instagram.com/{quote(username)}/"
    try:
        async with httpx.AsyncClient(
            headers=INSTAGRAM_HEADERS, follow_redirects=True, timeout=15
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
            if response.status_code == 200 and re.search(
                rf"instagram\.com/{escaped}/", body, re.IGNORECASE
            ):
                return STATUS_EXISTS
            return STATUS_UNKNOWN
    except Exception as e:
        print(f"Instagram error: {e}")
        return STATUS_UNKNOWN


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    active_users.add(user.id)
    welcome = (
        f"أهلاً بك يا {user.first_name} في **Aurora AI** 🚀\n\n"
        "💬 محادثة ذكية\n"
        "🌐 `/search موضوع`\n"
        "🎨 `/image وصف`\n"
        "🎙️ دز صوت\n"
        "📄 دز ملف\n"
        "📸 `/check username`\n"
        "🔔 `/watch username`\n"
        "📋 `/list`\n"
        "🧹 `/clear`"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in user_history:
        del user_history[chat_id]
    await update.message.reply_text("🧹 تم مسح المحادثة!")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_chat_users(update.effective_chat.id)
    await update.message.reply_text(
        f"📊 المستخدمون: `{len(active_users)}`\nمراقبة: `{len(rows)}`",
        parse_mode="Markdown",
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("الاستخدام:\n`/search موضوع`", parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    await update.message.chat.send_action(ChatAction.TYPING)
    search_data = await search_web(query)
    prompt = f"سؤال: {query}\n\nمعلومات:\n{search_data}\n\nاكتب إجابة منسقة."
    answer = await ask_ai_with_history(chat_id, prompt)
    await update.message.reply_text(answer, parse_mode="Markdown", disable_web_page_preview=True)


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("الاستخدام:\n`/image وصف`", parse_mode="Markdown")
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    try:
        encoded = urllib.parse.quote(prompt)
        image_url = f"https://pollinations.ai/p/{encoded}?width=1024&height=1024&seed=42"
        await update.message.reply_photo(
            photo=image_url,
            caption=f"🎨 _{prompt}_",
            parse_mode="Markdown",
        )
    except Exception as e:
        print("IMAGE ERROR:", e)
        await update.message.reply_text("❌ فشل توليد الصورة.")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/check username")
        return
    username = normalize_username(context.args[0])
    if not username:
        await update.message.reply_text("❌ اليوزر غير صحيح.")
        return
    await update.message.reply_text(f"🔎 فحص @{username}...")
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
    await update.message.reply_text(f"🔎 فحص @{username}...")
    status = await check_instagram_username(username)
    add_watched_user(chat_id, username, status)
    await update.message.reply_text(
        f"💾 تم حفظ @{username}\n{STATUS_TEXT[status]}\n⏱️ كل {MONITOR_INTERVAL // 60} دقائق."
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
        await update.message.reply_text(f"🗑️ حذفت @{username}.")
    else:
        await update.message.reply_text(f"⚠️ @{username} غير موجود.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_chat_users(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 لا توجد حسابات.")
        return
    lines = ["📋 *المراقبة:*\n"]
    for row in rows:
        u = row["username"]
        s = row["last_status"] or STATUS_UNKNOWN
        lines.append(f"• @{u} — {STATUS_TEXT.get(s, STATUS_UNKNOWN)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_text = update.message.text
    active_users.add(chat_id)
    if ai_rate_limited(chat_id):
        await update.message.reply_text("⏳ انتظر 3 ثواني.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    url_match = URL_PATTERN.search(user_text)
    if url_match:
        url = url_match.group(0)
        await update.message.reply_text("🔎 جاري تحليل الرابط...")
        prompt = f"حلل هذا الرابط وأعطني ملخصاً بالعربية: {url}"
        answer = await ask_ai_with_history(chat_id, prompt)
        await update.message.reply_text(answer, parse_mode="Markdown")
        return
    keywords = ["أخبار", "اخبار", "اليوم", "سعر", "مباراة", "الطقس", "ترند", "نتائج"]
    if search_client and any(w in user_text for w in keywords):
        web_data = await search_web(user_text)
        prompt = f"معلومات:\n{web_data}\n\nسؤال: {user_text}"
        answer = await ask_ai_with_history(chat_id, prompt)
    else:
        answer = await ask_ai_with_history(chat_id, user_text)
    if len(answer) > 4000:
        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i : i + 4000])
    else:
        await update.message.reply_text(answer, parse_mode="Markdown")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    chat_id = update.effective_chat.id
    active_users.add(chat_id)
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_bytes = await voice_file.download_as_bytearray()
        transcription = await ai_client.audio.transcriptions.create(
            file=("voice.ogg", bytes(voice_bytes)),
            model=WHISPER_MODEL,
            language="ar",
        )
        user_text = transcription.text.strip()
        if not user_text:
            await update.message.reply_text("❌ ما سمعتك.")
            return
        await update.message.reply_text(f"🗣️ _{user_text}_", parse_mode="Markdown")
        answer = await ask_ai_with_history(chat_id, user_text)
        await update.message.reply_text(answer, parse_mode="Markdown")
    except Exception as e:
        print("VOICE ERROR:", e)
        await update.message.reply_text("❌ خطأ في الصوت.")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    chat_id = update.effective_chat.id
    allowed = (".txt", ".py", ".json", ".md", ".csv", ".html", ".js")
    if not doc.file_name.lower().endswith(allowed):
        await update.message.reply_text("⚠️ TXT, PY, JSON, MD, CSV, HTML, JS فقط.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        file = await context.bot.get_file(doc.file_id)
        content_bytes = await file.download_as_bytearray()
        file_text = content_bytes.decode("utf-8", errors="ignore")[:4000]
        prompt = f"حلل الملف ({doc.file_name}):\n\n{file_text}"
        answer = await ask_ai_with_history(chat_id, prompt)
        await update.message.reply_text(answer, parse_mode="Markdown")
    except Exception as e:
        print("DOC ERROR:", e)
        await update.message.reply_text("❌ خطأ في الملف.")


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


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True)


def main():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    init_database()
    print("🚀 Aurora AI يعمل...")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("image", image_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("unwatch", unwatch_command))
    app.add_handler(CommandHandler("list", list_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    print("Bot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
```
