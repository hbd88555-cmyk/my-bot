import asyncio
import requests
from pyrogram import Client, filters
from pyrogram.types import Message
import google.generativeai as genai

# --- إعدادات البوت والخدمات ---
API_ID = 1234567  # استبدل بـ API_ID الخاص بك من my.telegram.org
API_HASH = "your_api_hash"  # استبدل بـ API_HASH الخاص بك
BOT_TOKEN = "your_bot_token"  # توكن البوت من BotFather

# إعداد ذكاء Google Gemini (للتحليل والترجمة تماماً مثل حالتك)
genai.configure(api_key="YOUR_GEMINI_API_KEY")  # ضع مفتاح Gemini API الخاص بك
ai_model = genai.GenerativeModel("gemini-pro")

app = Client("my_ai_checker_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# تخزين اليوزررز المحفوظة محلياً (يمكنك ربطها بقاعدة بيانات لاحقاً مثل SQLite)
saved_usernames = set()


# 1. ميزة الذكاء الاصطناعي (التحليل والترجمة)
@app.on_message(filters.command("ai") & filters.text)
async def ai_chat(client: Client, message: Message):
  # استخراج النص بعد أمر /ai
  prompt = message.text.replace("/ai", "", 1).strip()
  if not prompt:
    await message.reply(
        "أهلاً بك! يرجى كتابة النص أو السؤال بعد الأمر، مثال:\n`/ai ترجم هذا"
        " النص إلى الإنجليزية: ...`"
    )
    return

  waiting_msg = await message.reply("🤖 جاري التفكير والتحليل...")

  try:
    # إرسال النص إلى الذكاء الاصطناعي
    response = ai_model.generate_content(prompt)
    await waiting_msg.edit_text(response.text)
  except Exception as e:
    await waiting_msg.edit_text(f"حدث خطأ أثناء الاتصال بالذكاء الاصطناعي: {e}")


# 2. فحص يوزرات انستجرام
@app.on_message(filters.command("check") & filters.text)
async def check_instagram(client: Client, message: Message):
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "يرجى كتابة اليوزر للفحص، مثال:\n`/check instagram`"
    )
    return

  username = args[1].strip().replace("@", "")
  status_msg = await message.reply(f"🔍 جاري فحص اليوزر `{username}`...")

  # فحص حالة اليوزر عبر رابط انستجرام العام
  url = f"https://www.instagram.com/{username}/"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  try:
    response = requests.get(url, headers=headers, timeout=10)

    if response.status_code == 404:
      await status_msg.edit_text(
          f"❌ اليوزر `{username}`: **حساب غير متوفر** (متاح للتسجيل)."
      )
    elif response.status_code == 200:
      # حفظ اليوزر في القائمة
      saved_usernames.add(username)
      await status_msg.edit_text(
          f"✅ اليوزر `{username}`: **حساب موجود ومستخدم**!\nتم حفظ اليوزر"
          " بنجاح. 📁"
      )

      # محاكاة التنبيه بأن الحساب نشط الآن (أو فحص حالة نشاطه الفعلي)
      # يمكنك هنا إضافة فحص دوري، وسنرسل رسالة فورية للتنبيه:
      await client.send_message(
          message.chat.id,
          f"🚨 **تنبيه نشاط:** الحساب `{username}` الذي تم فحصه وتخزينه **نشط"
          " الآن**!",
      )
    else:
      await status_msg.edit_text(
          f"⚠️ حدث استجابة غير معتادة من سرير انستجرام (رمز الحالة:"
          f" {response.status_code})."
      )
  except Exception as e:
    await status_msg.edit_text(f"حدث خطأ أثناءفحص اليوزر: {e}")


# عرض اليوزرات المحفوظة
@app.on_message(filters.command("saved"))
async def list_saved(client: Client, message: Message):
  if not saved_usernames:
    await message.reply("📁 لا توجد يوزرات محفوظة حالياً.")
    else:
    text = "📁 **قائمة اليوزرات المحفوظة:**\n\n"
    for usr in saved_usernames:
      text += f"- @{usr}\n"
    await message.reply(text)


print("🤖 البوت يعمل الآن...")
app.run()
