Wexo Telegram Bot — Render deployment

1) ارفع محتويات هذا المجلد إلى مستودع GitHub جديد (ملفات الجذر مباشرة، وليس داخل مجلد إضافي).
2) في Render اختر New > Blueprint إذا أردت استخدام render.yaml، أو أنشئ Web Service من المستودع.
3) Build Command: pip install -r requirements.txt
   Start Command: python wexo_telegram_bot.py
4) أضف Environment Variables:
   WEXO_BOT_TOKEN = التوكن من @BotFather (سرّي، لا تضعه في الإضافة أو GitHub)
   WEXO_BOT_USERNAME = wexosystem_bot
   WEBHOOK_URL = عنوان الخدمة الكامل بعد نشرها، مثال https://wexo-telegram-bot.onrender.com (بدون / في النهاية)
   WEBHOOK_SECRET = قيمة عشوائية طويلة؛ إن استخدمت render.yaml يتم توليدها تلقائياً.
5) بعد وضع WEBHOOK_URL وWEBHOOK_SECRET أعد تشغيل الخدمة. سيضبط البوت Telegram webhook تلقائياً.
6) افتح https://YOUR-SERVICE.onrender.com/health ويجب أن يظهر {"ok": true, "service": "wexo-bot"}.
7) في ملف الإضافة scripts/wexo-config.js غيّر YOUR-RENDER-SERVICE إلى اسم خدمتك الحقيقي، ثم أعد ضغط مجلد Wexo_Enhancer لرفعه إلى Chrome Web Store.

مهم:
- لا تشغّل polling محلياً بالتوكن نفسه بينما webhook على Render؛ Telegram لا يسمح بالطريقتين معاً.
- Render Free قد يوقف الخدمة عند الخمول، فتتأخر أول استجابة عند إيقاظها. استخدم خطة دائمة إذا أردت استجابة ثابتة.
- قاعدة بيانات JSON داخل حاوية Render قد لا تبقى بعد إعادة النشر ما لم تضف Persistent Disk وتضبط مسار التخزين؛ لا تعتبرها تخزيناً دائماً حالياً.
- لا تضع التوكن في ملفات الإضافة أبداً.
