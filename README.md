# Discord Smart Reminder Bot (AR/EN)

بوت دسكورد ذكي لإدارة تذكيرات الفعاليات بدقة، مع دعم اللغة العربية والإنجليزية.

## الميزات

- إنشاء فعالية مع وقت محدد وتوقيت زمني مخصص.
- تحديد وقت التذكير قبل الفعالية بالدقائق (حرية كاملة للمستخدم).
- تعديل الفعالية لاحقاً: الوقت، نص الرسالة، صورة، نص إضافي، والقناة.
- إرسال التذكير تلقائياً عند اقتراب الفعالية.
- دعم ثنائي اللغة `Arabic / English` لكل مستخدم.
- حفظ البيانات محلياً في `SQLite` حتى بعد إعادة تشغيل البوت.

## English Summary

Smart and accurate Discord reminder bot with:

- User-defined event time and reminder lead time.
- Editable reminder content (message, image URL, extra text).
- Per-user language preference (`ar` / `en`).
- Persistent storage in SQLite.

## Project Files

- `bot.py`: Main bot logic and slash commands.
- `requirements.txt`: Python dependencies.
- `.env.example`: Environment variables example.

## Setup

1. Install Python 3.10+.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and put your token:

```env
DISCORD_BOT_TOKEN=your_real_token
```

4. Run the bot:

```bash
python bot.py
```

## Usage (Buttons)

- Use `/panel` to open the control panel.
- All actions are done via buttons for easy use.

Panel buttons:

- `Create Event | إنشاء`
	- Opens a modal to set event title, date/time, reminder minutes, timezone, and optional channel.

- `List Events | عرض`
	- Shows your upcoming events with IDs.

- `Edit Basic | تعديل أساسي`
	- Edit title/date/reminder minutes.

- `Edit Content | تعديل المحتوى`
	- Edit custom message, image URL, extra text, or clear them.

- `Delete | حذف`
	- Delete event by ID.

- `Language | اللغة`
	- Pick Arabic or English through language buttons.

- `Help | مساعدة`
	- Shows quick guidance.

## Date and Time Format

Recommended datetime input:

- `YYYY-MM-DD HH:MM`

Examples:

- `2026-04-15 20:30`
- `2026/04/15 20:30`

Timezone examples:

- `UTC`
- `Asia/Riyadh`
- `+03:00`

## Notes

- The bot checks reminders every 30 seconds.
- Reminders are sent in the chosen channel and mention the event creator.
- Use proper Discord bot permissions (Send Messages, Embed Links, Use Application Commands).