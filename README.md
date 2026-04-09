# Discord Smart Reminder Bot (AR/EN)

بوت دسكورد ذكي لإدارة تذكيرات الفعاليات بدقة، مع دعم اللغة العربية والإنجليزية.

---

## الميزات

- إنشاء فعاليات مع وقت محدد وأيام الأسبوع (قائمة اختيار).
- تحديد وقت التذكير قبل الفعالية بالدقائق.
- رفع صورة مرفقة من الجهاز (بدلاً من رابط).
- تعديل الفعالية: الوقت، الرسالة، الصورة، الأيام.
- حذف الفعاليات مباشرةً من لوحة التحكم.
- @everyone تلقائياً عند إرسال التذكير.
- لوحة اختيار لون الاسم (Color Roles) مع ألوان جاهزة وألوان مخصصة.
- دعم ثنائي اللغة `Arabic / English` لكل مستخدم.
- حفظ البيانات في `SQLite` حتى بعد إعادة تشغيل البوت.
- إعادة تشغيل تلقائية عند التوقف (watchdog script).

---

## English Summary

Smart Discord reminder bot:

- Create events with custom time, days, and reminder lead time (minute-precision).
- Upload event images from device (as Discord attachment).
- Full edit/delete via interactive button panels.
- @everyone mention on all reminders.
- Color role picker panel (preset + custom HEX colors).
- Per-user language: Arabic / English.
- SQLite persistence, auto-restart supervisor.

---

## Project Files

| File | Purpose |
|------|---------|
| `bot.py` | Main bot logic and slash commands |
| `requirements.txt` | Python dependencies |
| `run_bot_forever.sh` | Auto-restart supervisor (watchdog) |
| `.env.example` | Environment variables template |
| `test_bot_functionality.py` | Automated tests |

---

## Setup

### 1. Python

Requires **Python 3.10+** (Python 3.12 recommended).

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> **Important for hosting platforms:** The correct package is `discord.py` (not `discord`).
> Always run `pip install -r requirements.txt` and do **not** install the bare `discord` package.

### 3. Configure token

Copy `.env.example` to `.env` and paste your bot token:

```env
DISCORD_BOT_TOKEN=your_real_token_here
```

### 4. Run the bot

**Normal run:**
```bash
python bot.py
```

**With auto-restart (recommended for hosting):**
```bash
bash run_bot_forever.sh
```

---

## Bot Permissions Required

In Discord Developer Portal → Bot Permissions:

- `Send Messages`
- `Embed Links`
- `Attach Files`
- `Read Message History`
- `Use Application Commands`
- `Manage Roles` (for color roles feature)

Gateway Intents:
- `Message Content Intent` ✅
- `Server Members Intent` ✅

---

## Slash Commands

| Command | Description |
|---------|-------------|
| `/panel` | Open the main control panel |
| `/setup` | Configure reminder channel for this server |

---

## Control Panel Buttons

| Button | Description |
|--------|-------------|
| `Create Event \| إنشاء` | Create a new reminder event |
| `List Events \| عرض` | View your upcoming events |
| `Edit Message \| تعديل الرسالة` | Edit or delete any of your events |
| `Delete \| حذف` | Quick delete an event |
| `Language \| اللغة` | Switch between Arabic / English |
| `Settings \| الإعدادات` | Server settings (admin only) |
| `Help \| مساعدة` | Quick help |
| `Update Bot \| تحديث البوت` | Pull latest updates from GitHub (admin only) |

### Settings Buttons (Admin Only)

| Button | Description |
|--------|-------------|
| `Add Admin \| إضافة مشرف` | Grant admin access to a user |
| `Register Server \| تسجيل سيرفر` | Register a server and set reminder channel |
| `اختر لونك \| Choose Color` | Publish color picker panel to a channel |
| `إضافة لون \| Add Color` | Add a custom HEX color to the color panel |

---

## Color Roles Setup

1. Go to `Settings → اختر لونك | Choose Color`.
2. Select the channel to post the color picker panel in.
3. The bot creates color roles automatically and posts buttons.
4. Make sure the **bot's role is above all color roles** in Server Settings → Roles.

---

## Notes

- The bot checks reminders every **30 seconds**.
- Reminders are sent to the configured channel and mention `@everyone`.
- SQLite database (`events.db`) is created automatically on first run.
