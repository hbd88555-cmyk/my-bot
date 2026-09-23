# ============================================================
# Aurora Search AI - نسخة نظيفة بدون رموز
# Python 3.12+ | Railway Ready
# ============================================================

import os
import re
import time
import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from flask import Flask
from dotenv import load_dotenv
from openai import AsyncOpenAI

try:
    from duckduckgo_search import DDGS
    SEARCH_OK = True
except ImportError:
    SEARCH_OK = False
    print("duckduckgo-search غير مثبت")

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

BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN", "")
    or os.getenv("TELEGRAM_TOKEN", "")
).strip()

GROQ_API_KEY = (
    os.getenv("GROQ_API_KEY", "")
    or os.getenv("OPENAI_API_KEY", "")
).strip()

CHAT_MODEL = "openai/gpt-oss-120b"
BASE_URL = "https://api.groq.com/openai/v1"
DB_FILE = "aurora_search.sqlite3"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY غير موجود")

ai_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url=BASE_URL)


# ============================================================
# Flask
# ============================================================
flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health():
    return {"status": "ok", "bot": "Aurora Search"}, 200


# ============================================================
# قاعدة البيانات
# ============================================================
def db():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS watched (
            chat_id INTEGER, username TEXT, status TEXT, ts TEXT,
            PRIMARY KEY (chat_id, username)
        );
    """)
    c.commit()
    c.close()


def now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# أدوات
# ============================================================
URL_RE = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._]{1,30}$")

last_req = {}


def rate_ok(chat_id, secs=2):
    t = time.time()
    if t - last_req.get(chat_id, 0) < secs:
        return False
    last_req[chat_id] = t
    return True


def clean_text(text: str) -> str:
    """يشيل رموز Markdown من النص"""
    if not text:
        return ""
    # شيل # في بداية السطر
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # شيل ** و __
    text = text.replace("**", "").replace("__", "")
    # شيل الخطوط الفاصلة (---)
    text = re.sub(r"^-{2,}\s*$", "", text, flags=re.MULTILINE)
    # شيل رموز الكود
    text = text.replace("```", "").replace("`", "")
    # شيل النجوم في البداية
    text = re.sub(r"^\s*[*_]\s+", "", text, flags=re.MULTILINE)
    # تحويل [نص](رابط) إلى نص - رابط
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 - \2", text)
    # شيل المسافات الزايدة
    text = re.sub(r"\n{3,}", "\n\n", text)
    # شيل الأسطر الفاضية في البداية والنهاية
    return text.strip()


# ============================================================
# AI
# ============================================================
CLEAN_SYSTEM = (
    "أنت مساعد ذكي عربي ودود. "
    "اكتب بأسلوب طبيعي وسلس كأنك تحكي مع صديق. "
    "ممنوع تماماً استخدام أي رموز تنسيق مثل: # أو * أو _ أو ` أو --- أو [ ]. "
    "لا تستخدم عناوين بأقواس أو قوائم بنجوم. "
    "اكتب فقرات عادية فقط. "
    "إذا احتجت تسرد نقاط، استخدم أرقام عربية عادية (1، 2، 3) بدون أي رموز. "
    "أجب بالعربية بوضوح ودقة."
)


async def ask_ai(prompt, system=None):
    if not system:
        system = CLEAN_SYSTEM
    try:
        r = await ai_client.chat.completions.create(
            model=CHAT_MODEL,
            temperature=0.4,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        content = (r.choices[0].message.content or "").strip()
        if not content:
            return "ما وصلني رد."
        return clean_text(content)
    except Exception as e:
        print("AI ERROR:", e)
        return f"خطأ: {str(e)[:120]}"


# ============================================================
# كشف الحاجة للبحث
# ============================================================
SEARCH_KEYWORDS = [
    "أخبار", "اخبار", "اليوم", "الآن", "حالياً", "الجاري",
    "سعر", "أسعار", "كم سعر", "مباراة", "نتيجة", "نتائج",
    "الطقس", "الجو", "ترند", "تريند", "حديث", "جديد",
    "آخر", "أحدث", "2026", "2025", "هذا الأسبوع", "هذا الشهر",
    "news", "today", "price", "latest", "current",
]


def needs_search(text):
    t = text.lower()
    return any(kw.lower() in t for kw in SEARCH_KEYWORDS)


# ============================================================
# البحث في الويب
# ============================================================
def search_web(query, max_results=5):
    if not SEARCH_OK:
        return []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        print("SEARCH ERROR:", e)
        return []


async def fetch_page_text(url, max_chars=2500):
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "ar,en;q=0.9",
        }
        async with httpx.AsyncClient(
            headers=headers, timeout=12, follow_redirects=True
        ) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return ""
            html = r.text
            html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
            html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
            html = re.sub(r"<noscript.*?</noscript>", " ", html, flags=re.S | re.I)
            html = re.sub(r"<[^>]+>", " ", html)
            html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
            html = re.sub(r"&#\d+;", " ", html)
            html = re.sub(r"\s+", " ", html).strip()
            return html[:max_chars]
    except Exception as e:
        print(f"FETCH ERROR:", e)
        return ""


async def research_and_answer(chat_id, question, bot):
    """يبحث، يفتح الصفحات، يجيب، ويعطي المصادر"""
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except:
        pass

    results = search_web(question, max_results=5)
    if not results:
        return await ask_ai(question)

    pages = []
    for r in results[:3]:
        url = r.get("href", "")
        title = r.get("title", "")
        if not url:
            continue
        try:
            text = await fetch_page_text(url)
            if text and len(text) > 200:
                pages.append({"title": title, "url": url, "text": text})
        except:
            pass
        if len(pages) >= 3:
            break

    if pages:
        context = "المعلومات من الإنترنت:\n\n"
        for i, p in enumerate(pages, 1):
            context += f"مصدر {i}: {p['title']}\n"
            context += f"الرابط: {p['url']}\n"
            context += f"المحتوى: {p['text']}\n\n"

        prompt = (
            f"سؤال المستخدم: {question}\n\n"
            f"{context}\n\n"
            f"اكتب إجابة عربية منظمة بناءً على المصادر. "
            f"اذكر المصادر بالروابط في النهاية. "
            f"لا تستخدم أي رموز تنسيق."
        )
        answer = await ask_ai(prompt)

        sources = "\n\nالمصادر:\n"
        for i, p in enumerate(pages, 1):
            title = p["title"][:80]
            sources += f"{i}. {title}\n{p['url']}\n"

        return answer + sources
    else:
        txt = "نتائج البحث:\n\n"
        for i, r in enumerate(results, 1):
            title = r.get("title", "")[:80]
            url = r.get("href", "")
            body = r.get("body", "")[:150]
            txt += f"{i}. {title}\n{body}\n{url}\n\n"
        return clean_text(txt)


# ============================================================
# فحص إنستا
# ============================================================
async def check_insta(username):
    username = username.lower().replace("@", "").strip()
    if not USERNAME_RE.match(username):
        return "اليوزر غير صحيح"
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as c:
            r = await c.get(f"https://www.instagram.com/{quote(username)}/")
            body = r.text.lower()
            if r.status_code == 404:
                return "الحساب غير متوفر"
            for p in ["sorry, this page isn't available", "user not found", "page isn't available"]:
                if p in body:
                    return "الحساب غير متوفر"
            if r.status_code == 200 and re.search(rf"instagram\.com/{re.escape(username)}/", body, re.I):
                return "الحساب موجود"
            return "تعذر التحقق"
    except Exception as e:
        print("INSTA ERROR:", e)
        return "تعذر التحقق"


# ============================================================
# إرسال نصوص طويلة
# ============================================================
async def send_long(update, text):
    if not text:
        return
    text = clean_text(text)
    if len(text) <= 4000:
        try:
            await update.message.reply_text(text, disable_web_page_preview=True)
        except:
            await update.message.reply_text(text[:4000])
    else:
        parts = [text[i:i+3900] for i in range(0, len(text), 3900)]
        for part in parts:
            await update.message.reply_text(part, disable_web_page_preview=True)


# ============================================================
# الأوامر
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "أهلاً بك في Aurora Search AI\n\n"
        "أرسل أي سؤال، وإذا يحتاج بحث في الإنترنت، أبحث تلقائياً وأرد عليك مع المصادر.\n\n"
        "أمثلة:\n"
        "شنو صار اليوم بالعراق؟\n"
        "كم سعر البيتكوين؟\n"
        "من هو رئيس تركيا؟\n"
        "أحدث أخبار الذكاء الاصطناعي\n\n"
        "أوامر البحث:\n"
        "/search موضوع - بحث قسري\n"
        "/news موضوع - أخبار\n"
        "/opps مجال - فرص تقديم\n\n"
        "أوامر إنستا:\n"
        "/check username - فحص حساب\n"
        "/watch username - مراقبة\n"
        "/watching - قائمة المراقبة\n"
        "/unwatch username - إلغاء المراقبة\n\n"
        "/clear - مسح المحادثة"
    )
    await update.message.reply_text(txt)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/search موضوع")
        return
    query = " ".join(context.args)
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"جاري البحث عن: {query}...")
    answer = await research_and_answer(chat_id, query, context.bot)
    await send_long(update, answer)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/news موضوع")
        return
    query = " ".join(context.args)
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"جاري البحث عن أخبار: {query}...")
    answer = await research_and_answer(chat_id, f"{query} أخبار عاجلة", context.bot)
    await send_long(update, answer)


async def cmd_opps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = " ".join(context.args) if context.args else "عام"
    await update.message.reply_text(f"جاري البحث عن فرص في: {field}...")

    queries = [
        f"{field} منح دراسية",
        f"{field} وظائف شاغرة",
        f"{field} تدريب",
    ]

    txt = "فرص التقديم:\n\n"
    for q in queries:
        results = search_web(q, max_results=4)
        if results:
            txt += f"{q}:\n"
            for r in results[:3]:
                title = r.get("title", "")[:70]
                url = r.get("href", "")
                txt += f"{title}\n{url}\n"
            txt += "\n"

    await send_long(update, txt)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم مسح المحادثة.")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/check username")
        return
    uname = context.args[0]
    await update.message.reply_text(f"جاري فحص @{uname}...")
    status = await check_insta(uname)
    await update.message.reply_text(f"@{uname}\n{status}")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/watch username")
        return
    uname = context.args[0].lower().replace("@", "")
    if not USERNAME_RE.match(uname):
        await update.message.reply_text("اليوزر غير صحيح.")
        return
    await update.message.reply_text(f"جاري فحص @{uname}...")
    status = await check_insta(uname)
    c = db()
    c.execute("DELETE FROM watched WHERE chat_id = ? AND username = ?", (update.effective_chat.id, uname))
    c.execute("INSERT INTO watched VALUES (?, ?, ?, ?)", (update.effective_chat.id, uname, status, now()))
    c.commit()
    c.close()
    await update.message.reply_text(f"تم حفظ @{uname}\nالحالة: {status}\nسيتم فحصه كل 10 دقائق.")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("الاستخدام:\n/unwatch username")
        return
    uname = context.args[0].lower().replace("@", "")
    c = db()
    cur = c.execute("DELETE FROM watched WHERE chat_id = ? AND username = ?", (update.effective_chat.id, uname))
    c.commit()
    n = cur.rowcount
    c.close()
    if n:
        await update.message.reply_text(f"تم حذف @{uname}.")
    else:
        await update.message.reply_text("غير موجود.")


async def cmd_watching(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = db()
    rows = c.execute("SELECT username, status FROM watched WHERE chat_id = ?", (update.effective_chat.id,)).fetchall()
    c.close()
    if not rows:
        await update.message.reply_text("لا توجد حسابات مراقبة.")
        return
    txt = "الحسابات المراقبة:\n\n"
    for r in rows:
        txt += f"@{r['username']} - {r['status']}\n"
    await update.message.reply_text(txt)


# ============================================================
# المعالج الرئيسي
# ============================================================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    if not rate_ok(chat_id):
        await update.message.reply_text("انتظر شوي.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    url_match = URL_RE.search(text)
    if url_match:
        url = url_match.group(0)
        await update.message.reply_text("جاري فتح الرابط...")
        page_text = await fetch_page_text(url, max_chars=3000)
        if page_text:
            prompt = f"الرابط: {url}\n\nالمحتوى:\n{page_text}\n\nلخص لي بالعربية."
            ans = await ask_ai(prompt)
            await send_long(update, ans)
        else:
            ans = await research_and_answer(chat_id, text, context.bot)
            await send_long(update, ans)
        return

    if needs_search(text):
        await update.message.reply_text("جاري البحث...")
        ans = await research_and_answer(chat_id, text, context.bot)
        await send_long(update, ans)
        return

    ans = await ask_ai(text)
    await send_long(update, ans)


# ============================================================
# المراقبة
# ============================================================
async def monitor_loop(application):
    print("Monitor started.")
    await asyncio.sleep(20)
    counter = 0
    while True:
        try:
            counter += 60
            if counter >= 600:
                counter = 0
                c = db()
                rows = c.execute("SELECT chat_id, username, status FROM watched").fetchall()
                c.close()
                grouped = {}
                for row in rows:
                    grouped.setdefault(row["username"], []).append(row)

                for uname, watchers in grouped.items():
                    try:
                        status = await check_insta(uname)
                    except:
                        continue
                    if "تعذر" in status:
                        continue
                    for w in watchers:
                        prev = w["status"]
                        c2 = db()
                        c2.execute(
                            "UPDATE watched SET status = ?, ts = ? WHERE chat_id = ? AND username = ?",
                            (status, now(), w["chat_id"], uname),
                        )
                        c2.commit()
                        c2.close()
                        if "غير متوفر" in prev and "موجود" in status:
                            try:
                                await application.bot.send_message(
                                    chat_id=w["chat_id"],
                                    text=f"خبر حلو!\n\n@{uname} صار متوفراً على إنستغرام.\nhttps://instagram.com/{uname}",
                                )
                            except:
                                pass

            await asyncio.sleep(60)
        except Exception as e:
            print(f"Monitor error: {e}")
            await asyncio.sleep(60)


async def post_init(application):
    asyncio.create_task(monitor_loop(application))


# ============================================================
# Flask thread
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

    init_db()
    print("Aurora Search AI started...")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("opps", cmd_opps))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("watching", cmd_watching))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
# ============================================================
# دعم الصور - يوصف ويترجم محتوى الصورة
# ============================================================

import base64

# قائمة موديلات الرؤية (نجربهم بالترتيب حتى واحد يشتغل)
VISION_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.2-11b-vision-preview",
    "llama-3.2-90b-vision-preview",
    "qwen/qwen3-vl-27b-instruct",
]

VISION_PROMPT = (
    "اقرأ أي نص موجود داخل هذه الصورة، ثم ترجمه إلى العربية. "
    "إذا كانت الصورة تحتوي على مشهد، اوصفها بالتفصيل بالعربية. "
    "لا تستخدم رموز تنسيق مثل # أو * أو -. "
    "اكتب بأسلوب طبيعي وسلس."
)


async def try_vision_model(model_name, image_bytes, prompt):
    """يحاول موديل رؤية واحد"""
    try:
        b64 = base64.b64encode(image_bytes).decode()
        response = await ai_client.chat.completions.create(
            model=model_name,
            temperature=0.4,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                    ],
                }
            ],
        )
        content = response.choices[0].message.content if response.choices else None
        if content and content.strip():
            return content.strip()
        return None
    except Exception as e:
        print(f"VISION ({model_name}) ERROR: {str(e)[:150]}")
        return None


async def cmd_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج الصور: يوصف ويترجم محتواها"""
    if not update.message or not update.message.photo:
        return

    chat_id = update.effective_chat.id
    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("جاري قراءة الصورة...")

    try:
        # ناخذ أكبر حجم
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())

        # نص مرفق مع الصورة إن وجد
        caption = update.message.caption or ""
        if caption.strip():
            prompt = f"طلب المستخدم: {caption}\n\n{VISION_PROMPT}"
        else:
            prompt = VISION_PROMPT

        # نجرب كل الموديلات بالترتيب
        answer = None
        used_model = None
        for model in VISION_MODELS:
            print(f"Trying vision model: {model}")
            answer = await try_vision_model(model, image_bytes, prompt)
            if answer:
                used_model = model
                break

        if answer:
            answer = clean_text(answer)
            await send_long(update, answer)
        else:
            await update.message.reply_text(
                "ما كدرت أقرأ الصورة حالياً. "
                "يمكن موديلات الرؤية متوقفة من Groq. "
                "جرب مرة ثانية بعد شوي."
            )

    except Exception as e:
        print("PHOTO ERROR:", e)
        await update.message.reply_text("صار خطأ في معالجة الصورة.")
