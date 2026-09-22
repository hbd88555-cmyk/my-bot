import os
import threading
import requests
import time
import base64
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"
GROQ_COMPOUND_MODEL = "groq/compound-mini"

app = Flask(__name__)

# ===== التخزين =====
conversations = {}
personalities = {}
notes = {}
rate_limit = {}
watched_accounts = {}
checked_accounts = {}
watch_lock = threading.Lock()

# ===== الشخصيات =====
PERSONALITIES = {
    "default": """أنت مساعد ذكي محترف، تتكلم بالعربية بأسلوب واضح ومفيد.

قواعد:
1. فكر خطوة بخطوة قبل الرد.
2. إذا السؤال غامض، اطرح سؤال توضيحي واحد.
3. إذا ما تعرف، قل "ما أعرف" بلا اختراع.
4. استخدم أمثلة عملية.
5. نسّق الإجابات بنقاط إذا كانت طويلة.
6. إذا المستخدم كتب بلهجة عراقية، رد بنفس اللهجة.""",
    "funny": "أنت مساعد ساخر ومرح بلهجة عراقية. ترد بأسلوب مضحك بس مفيد.",
    "formal": "أنت مساعد رسمي ومهني. ترد بالفصحى بأسلوب مؤدب ومنظم.",
    "teacher": "أنت أستاذ صبور. تشرح خطوة بخطوة بأمثلة بسيطة.",
    "hacker": "أنت هاكر أخلاقي. ترد بأسلوب تقني مختصر بمصطلحات الأمن السيبراني.",
    "philosopher": "أنت فيلسوف متأمل. ترد بأسلوب عميق وتطرح أسئلة فكرية.",
    "doctor": "أنت مساعد طبي. تعطي نصائح صحية عامة وتنصح بمراجعة طبيب."
}

# ===== دوال تلغرام =====
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=30)
    except Exception as e:
        print("Send error:", e)

def send_chat_action(chat_id, action="typing"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": action}, timeout=10)
    except:
        pass

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=40)
        return r.json()
    except Exception as e:
        print("Get updates error:", e)
        return {}

def get_file_url(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
    r = requests.get(url, params={"file_id": file_id}, timeout=30)
    res = r.json()
    if res.get("ok"):
        return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{res['result']['file_path']}"
    return None

def download_file(file_id):
    file_url = get_file_url(file_id)
    if not file_url:
        return None
    r = requests.get(file_url, timeout=60)
    return r.content if r.status_code == 200 else None

def check_rate_limit(chat_id):
    now = time.time()
    if chat_id not in rate_limit:
        rate_limit[chat_id] = []
    rate_limit[chat_id] = [t for t in rate_limit[chat_id] if now - t < 3600]
    if len(rate_limit[chat_id]) >= 100:
        return False
    rate_limit[chat_id].append(now)
    return True

# ===== دوال Groq =====
def ask_groq(chat_id, user_text):
    personality = personalities.get(chat_id, "default")
    system = PERSONALITIES.get(personality, PERSONALITIES["default"])
    history = conversations.get(chat_id, [])
    messages = [{"role": "system", "content": system}]
    for msg in history[-12:]:
        messages.append(msg)
    messages.append({"role": "user", "content": user_text})
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    data = {"model": GROQ_MODEL, "messages": messages, "temperature": 0.7, "max_tokens": 2048, "top_p": 0.9}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=90)
        res = r.json()
        if "choices" in res:
            reply = res["choices"][0]["message"]["content"]
            conversations.setdefault(chat_id, []).append({"role": "user", "content": user_text})
            conversations[chat_id].append({"role": "assistant", "content": reply})
            conversations[chat_id] = conversations[chat_id][-24:]
            return reply
        return f"خطأ: {res.get('error', {}).get('message', 'غير معروف')}"
    except Exception as e:
        print("Groq error:", e)
        return "عذراً، صار خطأ. جرب مرة ثانية."

def ask_groq_vision(image_bytes, user_text):
    b64 = base64.b64encode(image_bytes).decode()
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = user_text if user_text else "اوصف هذه الصورة بالتفصيل بالعربية."
    data = {"model": GROQ_VISION_MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
    ]}], "temperature": 0.7, "max_tokens": 1500}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=90)
        res = r.json()
        return res["choices"][0]["message"]["content"] if "choices" in res else "ما كدرت أحلل الصورة."
    except Exception as e:
        print("Vision error:", e)
        return "صار خطأ في تحليل الصورة."

def transcribe_audio(audio_bytes, filename="audio.ogg"):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (filename, audio_bytes, "audio/ogg")}
    data = {"model": GROQ_WHISPER_MODEL, "language": "ar"}
    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        res = r.json()
        return res.get("text")
    except Exception as e:
        print("Whisper error:", e)
        return None

def check_instagram(username):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""ابحث في الويب عن حساب إنستغرام بالاسم: {username}

تحقق من:
1. هل الصفحة موجودة على instagram.com/{username}؟
2. إذا موجودة، هل الحساب نشط أم معطل؟

أجب بكلمة واحدة فقط:
- "active" إذا الحساب موجود ونشط
- "disabled" إذا الحساب موجود لكن معطل
- "not_found" إذا الحساب غير موجود

لا تكتب أي شي ثاني."""
    data = {"model": GROQ_COMPOUND_MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2, "max_tokens": 200}
    try:
        r = requests.post(url, headers=headers, json=data, timeout=60)
        res = r.json()
        if "choices" not in res:
            print("Compound response:", res)
            return None
        text = res["choices"][0]["message"]["content"].lower().strip()
        if "active" in text:
            return "active"
        if "disabled" in text:
            return "disabled"
        if "not_found" in text or "not found" in text:
            return "not_found"
        return None
    except Exception as e:
        print("Compound error:", e)
        return None

# ===== قائمة إنستغرام =====
def save_to_list(chat_id, username, status):
    if chat_id not in checked_accounts:
        checked_accounts[chat_id] = {}
    checked_accounts[chat_id][username] = {
        "status": status,
        "time": time.strftime("%Y-%m-%d %H:%M")
    }

def show_list(chat_id):
    accounts = checked_accounts.get(chat_id, {})
    if not accounts:
        send_message(chat_id, "📋 قائمتك فاضية.\n\nاكتب: /insta اسم_المستخدم")
        return
    active, disabled, not_found = [], [], []
    for u, info in accounts.items():
        if info["status"] == "active":
            active.append(u)
        elif info["status"] == "disabled":
            disabled.append(u)
        else:
            not_found.append(u)
    msg = f"📋 قائمة حسابات إنستغرام ({len(accounts)})\n\n"
    if active:
        msg += f"✅ نشطة ({len(active)}):\n"
        for u in active:
            msg += f"• @{u}\n"
        msg += "\n"
    if disabled:
        msg += f"⚠️ معطلة ({len(disabled)}):\n"
        for u in disabled:
            msg += f"• @{u}\n"
        msg += "\n"
    if not_found:
        msg += f"❌ غير موجودة ({len(not_found)}):\n"
        for u in not_found:
            msg += f"• @{u}\n"
    msg += "\n💡 /insta del اسم — يحذف\n"
    msg += "💡 /insta clear — يمسح الكل"
    send_message(chat_id, msg)

# ===== نظام المراقبة =====
def add_to_watch(username, chat_id):
    with watch_lock:
        watched_accounts.setdefault(username, set()).add(chat_id)

def remove_from_watch(username, chat_id):
    with watch_lock:
        if username in watched_accounts:
            watched_accounts[username].discard(chat_id)
            if not watched_accounts[username]:
                del watched_accounts[username]

def watcher_loop():
    print("Watcher started...")
    while True:
        try:
            time.sleep(1800)
            with watch_lock:
                items = list(watched_accounts.items())
            if not items:
                continue
            print(f"Checking {len(items)} watched accounts...")
            for username, chat_ids in items:
                status = check_instagram(username)
                if status == "active":
                    for cid in list(chat_ids):
                        send_message(cid, f"🎉 خبر حلو!\n\nحساب @{username} صار متاح!\n\n🔗 https://instagram.com/{username}")
                        save_to_list(cid, username, "active")
                    with watch_lock:
                        if username in watched_accounts:
                            del watched_accounts[username]
                elif status == "disabled":
                    for cid in list(chat_ids):
                        save_to_list(cid, username, "disabled")
        except Exception as e:
            print("Watcher error:", e)
            time.sleep(60)

# ===== الأوامر =====
def handle_command(chat_id, text):
    if text == "/start":
        send_message(chat_id, "أهلاً بك! 👋\n\nاكتب /help عشان تشوف الأوامر.")
        return True
    if text == "/help":
        send_message(chat_id, """📋 الأوامر:

/start — ترحيب
/help — المساعدة
/about — عن البوت
/clear — يمسح المحادثة
/personality — يغير الشخصية
/stats — إحصائياتك

📝 الملاحظات:
/note نص — يحفظ
/notes — عرض
/delnote رقم — حذف

🌐 /translate نص — ترجمة

📸 إنستغرام:
/insta اسم — يفحص ويضيف للقائمة
/insta — يعرض القائمة
/insta del اسم — يحذف
/insta clear — يمسح الكل

🔔 المراقبة:
/watch اسم — يخبرك متى يصير متاح
/unwatch اسم — إلغاء
/watching — قائمة المراقبة

🖼️ دز صورة | 🎤 دز صوت""")
        return True
    if text == "/about":
        send_message(chat_id, "🤖 بوت ذكي متقدم\n\n⚡ Llama 3.3 70B\n• محادثة ذكية\n• صور وصوت\n• 7 شخصيات\n• قائمة إنستغرام\n• مراقبة تلقائية\n\nصُنع بـ ❤️")
        return True
    if text == "/clear":
        conversations[chat_id] = []
        send_message(chat_id, "✅ تم مسح المحادثة.")
        return True
    if text == "/personality":
        current = personalities.get(chat_id, "default")
        msg = f"🎭 شخصيتك: {current}\n\nالمتاحة:\n"
        for p in PERSONALITIES:
            msg += f"• {p}\n"
        msg += "\n/personality اسم_الشخصية"
        send_message(chat_id, msg)
        return True
    if text.startswith("/personality "):
        p = text.replace("/personality ", "").strip().lower()
        if p in PERSONALITIES:
            personalities[chat_id] = p
            send_message(chat_id, f"✅ تم تغيير الشخصية إلى: {p}")
        else:
            send_message(chat_id, "❌ شخصية غير معروفة.")
        return True
    if text.startswith("/note "):
        note = text.replace("/note ", "", 1).strip()
        notes.setdefault(chat_id, []).append(note)
        send_message(chat_id, f"✅ تم حفظ الملاحظة ({len(notes[chat_id])})")
        return True
    if text == "/notes":
        if notes.get(chat_id):
            msg = "📝 ملاحظاتك:\n\n"
            for i, n in enumerate(notes[chat_id], 1):
                msg += f"{i}. {n}\n"
            send_message(chat_id, msg)
        else:
            send_message(chat_id, "ما عندك ملاحظات.")
        return True
    if text.startswith("/delnote "):
        try:
            num = int(text.replace("/delnote ", "").strip())
            if notes.get(chat_id) and 1 <= num <= len(notes[chat_id]):
                deleted = notes[chat_id].pop(num - 1)
                send_message(chat_id, f"✅ تم حذف: {deleted}")
            else:
                send_message(chat_id, "❌ رقم غير صحيح.")
        except:
            send_message(chat_id, "❌ اكتب رقم صحيح.")
        return True
    if text.startswith("/translate "):
        content = text.replace("/translate ", "", 1).strip()
        reply = ask_groq(chat_id, f"ترجم هذا النص إلى العربية فقط بدون أي إضافة أو شرح: {content}")
        send_message(chat_id, reply)
        return True

    # إنستغرام
    if text == "/insta":
        show_list(chat_id)
        return True
    if text.startswith("/insta clear"):
        checked_accounts[chat_id] = {}
        send_message(chat_id, "✅ تم مسح القائمة كاملة.")
        return True
    if text.startswith("/insta del "):
        username = text.replace("/insta del ", "").strip().replace("@", "").lower()
        if chat_id in checked_accounts and username in checked_accounts[chat_id]:
            del checked_accounts[chat_id][username]
            send_message(chat_id, f"✅ تم حذف @{username}.")
        else:
            send_message(chat_id, f"❌ @{username} مو في القائمة.")
        return True
    if text.startswith("/insta "):
        username = text.replace("/insta ", "").strip().replace("@", "").lower()
        if not username:
            show_list(chat_id)
            return True
        send_chat_action(chat_id, "typing")
        send_message(chat_id, f"🔍 جاري فحص @{username}...")
        status = check_instagram(username)
        if status == "active":
            save_to_list(chat_id, username, "active")
            send_message(chat_id, f"✅ @{username} — موجود ومتاح حالياً\n🔗 https://instagram.com/{username}\n\n💡 أضفته لقائمتك")
        elif status == "disabled":
            save_to_list(chat_id, username, "disabled")
            send_message(chat_id, f"⚠️ @{username} — معطل حالياً\n\n💡 أضفته لقائمتك\n🔔 اكتب /watch {username} وراح أخبرك متى يصير متاح")
        elif status == "not_found":
            save_to_list(chat_id, username, "not_found")
            send_message(chat_id, f"❌ @{username} — غير موجود\n\n💡 أضفته لقائمتك")
        else:
            send_message(chat_id, "⚠️ ما كدرت أفحص. جرب بعد شوية.")
        return True

    # المراقبة
    if text.startswith("/watch "):
        username = text.replace("/watch ", "").strip().replace("@", "").lower()
        if not username:
            send_message(chat_id, "❌ مثال: /watch cristiano")
            return True
        send_chat_action(chat_id, "typing")
        status = check_instagram(username)
        if status == "active":
            save_to_list(chat_id, username, "active")
            send_message(chat_id, f"✅ @{username} متاح حالياً أصلاً!\n🔗 https://instagram.com/{username}")
        elif status == "disabled":
            add_to_watch(username, chat_id)
            save_to_list(chat_id, username, "disabled")
            send_message(chat_id, f"🔔 راح أراقب @{username} وأخبرك أول ما يصير متاح.\n⏱️ الفحص كل 30 دقيقة.")
        elif status == "not_found":
            add_to_watch(username, chat_id)
            save_to_list(chat_id, username, "not_found")
            send_message(chat_id, f"🔔 راح أراقب @{username} حتى لو غير موجود.\n⏱️ الفحص كل 30 دقيقة.")
        else:
            send_message(chat_id, "⚠️ ما كدرت أفحص. جرب بعد شوية.")
        return True
    if text.startswith("/unwatch "):
        username = text.replace("/unwatch ", "").strip().replace("@", "").lower()
        remove_from_watch(username, chat_id)
        send_message(chat_id, f"✅ تم إلغاء مراقبة @{username}")
        return True
    if text == "/watching":
        with watch_lock:
            mine = [u for u, ids in watched_accounts.items() if chat_id in ids]
        if mine:
            msg = "📋 الحسابات المراقبة:\n\n"
            for u in mine:
                msg += f"• @{u}\n"
            send_message(chat_id, msg)
        else:
            send_message(chat_id, "ما عندك حسابات مراقبة.")
        return True
    if text == "/stats":
        msg_count = len(conversations.get(chat_id, [])) // 2
        notes_count = len(notes.get(chat_id, []))
        p = personalities.get(chat_id, "default")
        with watch_lock:
            watching = len([u for u, ids in watched_accounts.items() if chat_id in ids])
        insta_count = len(checked_accounts.get(chat_id, {}))
        send_message(chat_id, f"📊 إحصائياتك:\n\n💬 الرسائل: {msg_count}\n📝 الملاحظات: {notes_count}\n📸 حسابات إنستا: {insta_count}\n🔔 المراقبة: {watching}\n🎭 الشخصية: {p}")
        return True
    return False

# ===== معالجة الرسائل =====
def process_message(chat_id, text):
    if not check_rate_limit(chat_id):
        send_message(chat_id, "⏳ تجاوزت الحد (100/ساعة).")
        return
    if handle_command(chat_id, text):
        return
    send_chat_action(chat_id, "typing")
    send_message(chat_id, ask_groq(chat_id, text))

def process_photo(chat_id, photo_sizes, caption):
    send_chat_action(chat_id, "typing")
    image_bytes = download_file(photo_sizes[-1]["file_id"])
    if not image_bytes:
        send_message(chat_id, "ما كدرت أحمّل الصورة.")
        return
    send_message(chat_id, ask_groq_vision(image_bytes, caption))

def process_voice(chat_id, voice):
    send_chat_action(chat_id, "typing")
    audio_bytes = download_file(voice["file_id"])
    if not audio_bytes:
        send_message(chat_id, "ما كدرت أحمّل الصوت.")
        return
    text = transcribe_audio(audio_bytes)
    if not text:
        send_message(chat_id, "ما كدرت أفهم الصوت.")
        return
    send_message(chat_id, f"🎤 سمعت: {text}")
    send_chat_action(chat_id, "typing")
    send_message(chat_id, ask_groq(chat_id, text))

# ===== الحلقات =====
def telegram_loop():
    print("Bot started...")
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            if updates.get("result"):
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        continue
                    chat_id = message["chat"]["id"]
                    if "text" in message:
                        print(f"User {chat_id}: {message['text']}")
                        process_message(chat_id, message["text"])
                    elif "photo" in message:
                        process_photo(chat_id, message["photo"], message.get("caption", ""))
                    elif "voice" in message:
                        process_voice(chat_id, message["voice"])
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(5)

_bot_started = False
def start_threads():
    global _bot_started
    if not _bot_started:
        _bot_started = True
        threading.Thread(target=telegram_loop, daemon=True).start()
        threading.Thread(target=watcher_loop, daemon=True).start()

start_threads()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
