import os
import threading
import requests
import time
from flask import Flask

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.1-8b-instant"

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    except Exception as e:
        print("Send error:", e)

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

def ask_groq(user_text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "أنت مساعد ذكي ترد بالعربية بأسلوب ودود ومفيد ومختصر."},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.7
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=30)
        res = r.json()
        return res["choices"][0]["message"]["content"]
    except Exception as e:
        print("Groq error:", e)
        return "عذراً، صار خطأ. جرب مرة ثانية."

def telegram_loop():
    print("Bot started...")
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            if updates.get("result"):
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update and "text" in update["message"]:
                        chat_id = update["message"]["chat"]["id"]
                        user_text = update["message"]["text"]
                        print(f"User: {user_text}")
                        reply = ask_groq(user_text)
                        print(f"Bot: {reply}")
                        send_message(chat_id, reply)
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(5)

# Start bot in background thread when module loads
_bot_started = False
def start_bot_thread():
    global _bot_started
    if not _bot_started:
        _bot_started = True
        t = threading.Thread(target=telegram_loop, daemon=True)
        t.start()

start_bot_thread()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
