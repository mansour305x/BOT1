import asyncio
import datetime as dt
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
import zipfile
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

DB_PATH = "events.db"
CHECK_INTERVAL_SECONDS = 30
BOT_OWNER_ID = 1376784524016619551
GITHUB_REPO_OWNER = "mansour305x"
GITHUB_REPO_NAME = "BOT1"
GITHUB_REPO_BRANCH = "main"

# Predefined times (00:00 to 23:30 in 30-minute intervals) + 24:00
TIMES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)] + ["24:00"]
REMINDER_MINUTES_OPTIONS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]

COLOR_PRESETS = [
    ("أحمر", "FF4D4D", "🔴"),
    ("برتقالي", "FF8C42", "🟠"),
    ("أصفر", "FFD93D", "🟡"),
    ("أخضر", "4CD964", "🟢"),
    ("أزرق", "3498DB", "🔵"),
    ("بنفسجي", "9B59B6", "🟣"),
    ("وردي", "FF69B4", "🩷"),
    ("أبيض", "ECF0F1", "⚪"),
    ("أسود", "2C3E50", "⚫"),
]

# Values are based on Python weekday(): Monday=0 ... Sunday=6
DAY_NAME_BY_VALUE = {
    "0": "الاثنين",
    "1": "الثلاثاء",
    "2": "الأربعاء",
    "3": "الخميس",
    "4": "الجمعة",
    "5": "السبت",
    "6": "الأحد",
}


def format_days_summary(days_csv: str) -> str:
    if days_csv == "alt":
        return "يوم إيه / يوم لا"
    tokens = [d for d in days_csv.split(",") if d in DAY_NAME_BY_VALUE]
    if tokens == ["0", "1", "2", "3", "4", "5", "6"]:
        return "كل الأيام"
    if not tokens:
        return "-"
    return "، ".join(DAY_NAME_BY_VALUE[d] for d in tokens)


def get_alt_cycle_start_text(created_at_iso: Optional[str]) -> str:
    """Return human-readable text for alternate day cycle start."""
    if not created_at_iso:
        return "من اليوم"
    try:
        start_date = dt.datetime.fromisoformat(created_at_iso).date()
        return f"يبدأ من {start_date.strftime('%d/%m/%Y')}"
    except Exception:
        return "من اليوم"


def is_every_other_day_active(created_at_iso: Optional[str], target_date: dt.date) -> bool:
    """Return True when target_date matches the alternating-day cycle start."""
    if not created_at_iso:
        return True
    try:
        start_date = dt.datetime.fromisoformat(created_at_iso).date()
    except Exception:
        return True
    return (target_date - start_date).days % 2 == 0


def parse_event_time(time_value: str) -> tuple[int, int, bool]:
    """Parse stored event time and return (hour, minute, add_one_day)."""
    if time_value == "24:00":
        return 0, 0, True

    hour, minute = map(int, time_value.split(":"))
    return hour, minute, False

MESSAGES = {
    "en": {
        "lang_set": "Language updated to English.",
        "invalid_lang": "Invalid language. Use `en` or `ar`.",
        "invalid_time_choice": "Invalid time. Use allowed values like 00:00, 00:30 ... 23:30 or 24:00.",
        "time_in_past": "Event time must be in the future.",
        "reminder_in_past": "Reminder time is already in the past.",
        "event_created": "Event created successfully. ID: **{event_id}**.",
        "event_not_found": "Event not found, or you do not have permission.",
        "event_updated": "Event updated successfully.",
        "event_deleted": "Event deleted successfully.",
        "no_events": "No events found.",
        "events_header": "Your upcoming events:",
        "invalid_image": "Image URL must start with http:// or https://",
        "reminder_sent_log": "Reminder sent for event #{event_id}",
        "channel_missing": "Target channel not found.",
        "settings_updated": "Server settings updated.",
        "help": "Use `/panel` to open control panel.",
        "event_reminder_title": "Event Reminder",
        "event_field_name": "Event",
        "select_time": "Select Time",
        "select_days": "Select Days",
        "select_message": "Edit Message",
        "admin_added": "Admin added.",
        "role_set": "Notification role set.",
    },
    "ar": {
        "lang_set": "تم تحديث اللغة إلى العربية.",
        "invalid_lang": "لغة غير صحيحة.",
        "invalid_time_choice": "وقت غير صحيح. استخدم قيمة مسموحة مثل 00:00 أو 00:30 إلى 23:30 أو 24:00.",
        "time_in_past": "الوقت يجب أن يكون في المستقبل.",
        "reminder_in_past": "وقت التذكير في الماضي.",
        "event_created": "تم إنشاء الفعالية بنجاح. المعرّف: **{event_id}**.",
        "event_not_found": "الفعالية غير موجودة.",
        "event_updated": "تم تعديل الفعالية بنجاح.",
        "event_deleted": "تم حذف الفعالية بنجاح.",
        "no_events": "لا توجد فعاليات.",
        "events_header": "فعالياتك القادمة:",
        "invalid_image": "رابط الصورة غير صحيح.",
        "reminder_sent_log": "تم إرسال تذكير للفعالية #{event_id}",
        "channel_missing": "القناة غير موجودة.",
        "settings_updated": "تم تحديث الإعدادات.",
        "help": "استخدم `/panel` لفتح لوحة التحكم.",
        "event_reminder_title": "تذكير فعالية",
        "event_field_name": "الفعالية",
        "select_time": "اختر الوقت",
        "select_days": "اختر الأيام",
        "select_message": "عدّل الرسالة",
        "admin_added": "تم إضافة مشرف.",
        "role_set": "تم تعيين الرول.",
    },
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            time TEXT NOT NULL,
            days TEXT NOT NULL,
            remind_before_minutes INTEGER NOT NULL DEFAULT 10,
            message TEXT,
            image_url TEXT,
            last_sent_marker TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Lightweight migration for existing databases.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "remind_before_minutes" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN remind_before_minutes INTEGER NOT NULL DEFAULT 10")
    if "last_sent_marker" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN last_sent_marker TEXT")
    if "channel_id" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN channel_id INTEGER")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL DEFAULT 'en'
        )
    """)
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS server_settings (
            guild_id INTEGER PRIMARY KEY,
            notification_role_id INTEGER,
            notification_channel_id INTEGER
        )
    """)

    # Migration for server_settings
    ss_columns = {row["name"] for row in conn.execute("PRAGMA table_info(server_settings)").fetchall()}
    if "notification_channel_id" not in ss_columns:
        conn.execute("ALTER TABLE server_settings ADD COLUMN notification_channel_id INTEGER")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS registered_servers (
            guild_id INTEGER PRIMARY KEY,
            guild_name TEXT NOT NULL,
            guild_owner_id INTEGER,
            registered_by INTEGER NOT NULL,
            registered_at TEXT NOT NULL,
            last_channel_sync_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS registered_server_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS color_roles (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            color_hex TEXT NOT NULL,
            label TEXT NOT NULL,
            emoji TEXT NOT NULL,
            PRIMARY KEY (guild_id, role_id)
        )
    """)
    
    conn.commit()
    return conn


def get_user_lang(user_id: int) -> str:
    conn = get_conn()
    try:
        row = conn.execute("SELECT lang FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row["lang"] if row and row["lang"] in MESSAGES else "en"
    finally:
        conn.close()


def set_user_lang(user_id: int, lang: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO user_settings (user_id, lang) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
            (user_id, lang),
        )
        conn.commit()
    finally:
        conn.close()


def t(user_id: int, key: str, **kwargs) -> str:
    lang = get_user_lang(user_id)
    template = MESSAGES.get(lang, MESSAGES["en"]).get(key, key)
    return template.format(**kwargs)


def is_bot_owner(user_id: int) -> bool:
    return user_id == BOT_OWNER_ID


def is_guild_admin(guild_id: int, user_id: int) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def has_guild_admin_access(guild_id: int, user_id: int, guild_owner_id: int) -> bool:
    if is_bot_owner(user_id):
        return True
    if user_id == guild_owner_id:
        return True
    return is_guild_admin(guild_id, user_id)


def can_manage_server_settings(user_id: int) -> bool:
    return is_bot_owner(user_id)


def register_server_record(guild: discord.Guild, registered_by: int) -> None:
    conn = get_conn()
    try:
        now_iso = dt.datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO registered_servers (
                guild_id, guild_name, guild_owner_id, registered_by, registered_at, last_channel_sync_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                guild_name = excluded.guild_name,
                guild_owner_id = excluded.guild_owner_id,
                registered_by = excluded.registered_by
            """,
            (guild.id, guild.name, guild.owner_id, registered_by, now_iso, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def sync_registered_server_channels(guild: discord.Guild) -> int:
    text_channels = sorted(guild.text_channels, key=lambda c: c.position)
    now_iso = dt.datetime.now().isoformat()

    conn = get_conn()
    try:
        for idx, ch in enumerate(text_channels):
            conn.execute(
                """
                INSERT INTO registered_server_channels (
                    guild_id, channel_id, channel_name, position, is_active, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                    channel_name = excluded.channel_name,
                    position = excluded.position,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (guild.id, ch.id, ch.name, idx, now_iso),
            )

        existing_rows = conn.execute(
            "SELECT channel_id FROM registered_server_channels WHERE guild_id = ?",
            (guild.id,),
        ).fetchall()
        existing_ids = {row["channel_id"] for row in existing_rows}
        active_ids = {ch.id for ch in text_channels}
        for stale_id in existing_ids - active_ids:
            conn.execute(
                """
                UPDATE registered_server_channels
                SET is_active = 0, updated_at = ?
                WHERE guild_id = ? AND channel_id = ?
                """,
                (now_iso, guild.id, stale_id),
            )

        conn.execute(
            "UPDATE registered_servers SET last_channel_sync_at = ?, guild_name = ?, guild_owner_id = ? WHERE guild_id = ?",
            (now_iso, guild.name, guild.owner_id, guild.id),
        )
        conn.commit()
    finally:
        conn.close()

    return len(text_channels)


def is_server_registered(guild_id: int) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM registered_servers WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def get_registered_server_channels(guild_id: int, only_active: bool = True) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if only_active:
            rows = conn.execute(
                """
                SELECT channel_id, channel_name, position
                FROM registered_server_channels
                WHERE guild_id = ? AND is_active = 1
                ORDER BY position ASC, channel_name ASC
                """,
                (guild_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT channel_id, channel_name, position, is_active
                FROM registered_server_channels
                WHERE guild_id = ?
                ORDER BY position ASC, channel_name ASC
                """,
                (guild_id,),
            ).fetchall()
        return rows
    finally:
        conn.close()


def ensure_server_settings_row(guild_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO server_settings (guild_id)
            VALUES (?)
            ON CONFLICT(guild_id) DO NOTHING
            """,
            (guild_id,),
        )
        conn.commit()
    finally:
        conn.close()


def register_current_server(guild: discord.Guild, registered_by: int) -> int:
    ensure_server_settings_row(guild.id)
    register_server_record(guild, registered_by)
    return sync_registered_server_channels(guild)


def validate_image_url(url: Optional[str]) -> bool:
    if url is None or url == "":
        return True
    return url.startswith("http://") or url.startswith("https://")


async def run_bot_update() -> tuple[bool, str]:
    bot_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()

    if os.path.isdir(os.path.join(bot_dir, ".git")):
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=bot_dir,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout.decode().strip() or stderr.decode().strip())[:1200]
        if proc.returncode != 0:
            return False, f"Git update failed:\n{output}"

        pip_proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=bot_dir,
        )
        pip_stdout, pip_stderr = await pip_proc.communicate()
        pip_output = (pip_stdout.decode().strip() or pip_stderr.decode().strip())[-800:]
        if pip_proc.returncode != 0:
            return False, f"Dependency install failed:\n{pip_output}"

        return True, f"{output}\n\nDependencies:\n{pip_output}"

    zip_url = f"https://codeload.github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/zip/refs/heads/{GITHUB_REPO_BRANCH}"
    keep_names = {".env", "events.db", "bot.log", "bot.out", "__pycache__"}

    def download_and_extract() -> str:
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "repo.zip")
            urllib.request.urlretrieve(zip_url, zip_path)

            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(temp_dir)

            extracted_root = None
            for name in os.listdir(temp_dir):
                candidate = os.path.join(temp_dir, name)
                if os.path.isdir(candidate) and name.startswith(f"{GITHUB_REPO_NAME}-"):
                    extracted_root = candidate
                    break

            if extracted_root is None:
                raise RuntimeError("Unable to locate extracted repository files.")

            for entry in os.listdir(extracted_root):
                if entry in keep_names:
                    continue

                src = os.path.join(extracted_root, entry)
                dst = os.path.join(bot_dir, entry)

                if os.path.isdir(dst) and not os.path.islink(dst):
                    shutil.rmtree(dst)
                elif os.path.exists(dst):
                    os.remove(dst)

                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        return "Downloaded latest GitHub source archive and replaced bot files."

    try:
        update_output = await asyncio.to_thread(download_and_extract)
    except Exception as exc:
        return False, f"Archive update failed:\n{exc}"

    pip_proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        "requirements.txt",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=bot_dir,
    )
    pip_stdout, pip_stderr = await pip_proc.communicate()
    pip_output = (pip_stdout.decode().strip() or pip_stderr.decode().strip())[-800:]
    if pip_proc.returncode != 0:
        return False, f"Dependency install failed:\n{pip_output}"

    return True, f"{update_output}\n\nDependencies:\n{pip_output}"


async def ensure_color_roles(guild: discord.Guild):
    conn = get_conn()
    ensured_map = {}
    try:
        for label, color_hex, emoji in COLOR_PRESETS:
            row = conn.execute(
                "SELECT role_id FROM color_roles WHERE guild_id = ? AND color_hex = ?",
                (guild.id, color_hex),
            ).fetchone()

            role = guild.get_role(row["role_id"]) if row else None
            if role is None:
                role = await guild.create_role(
                    name=f"Color | {label}",
                    colour=discord.Colour(int(color_hex, 16)),
                    mentionable=False,
                    reason="Create color role panel roles",
                )

            conn.execute(
                """
                INSERT INTO color_roles (guild_id, role_id, color_hex, label, emoji)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, role_id) DO UPDATE SET
                    color_hex = excluded.color_hex,
                    label = excluded.label,
                    emoji = excluded.emoji
                """,
                (guild.id, role.id, color_hex, label, emoji),
            )
            ensured_map[role.id] = (role.id, label, emoji)

        # Include custom colors that were added later from settings.
        custom_rows = conn.execute(
            "SELECT role_id, label, emoji FROM color_roles WHERE guild_id = ?",
            (guild.id,),
        ).fetchall()
        for row in custom_rows:
            role = guild.get_role(row["role_id"])
            if role is not None:
                ensured_map[row["role_id"]] = (row["role_id"], row["label"], row["emoji"])

        conn.commit()
        return list(ensured_map.values())
    finally:
        conn.close()


def build_color_picker_view(guild_id: int, role_entries):
    view = discord.ui.View(timeout=None)

    for idx, (role_id, label, emoji) in enumerate(role_entries):
        btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji=emoji,
            custom_id=f"pick-color:{guild_id}:{role_id}",
            row=idx // 5,
        )

        async def on_click(interaction: discord.Interaction, selected_role_id=role_id, selected_label=label):
            if not interaction.guild or interaction.guild.id != guild_id:
                await interaction.response.send_message("هذه اللوحة لا تخص هذا السيرفر.", ephemeral=True)
                return

            member = interaction.guild.get_member(interaction.user.id)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(interaction.user.id)
                except Exception:
                    await interaction.response.send_message("لم أستطع العثور على العضو.", ephemeral=True)
                    return

            selected_role = interaction.guild.get_role(selected_role_id)
            if selected_role is None:
                await interaction.response.send_message("الرول المحدد غير موجود.", ephemeral=True)
                return

            move_warning = None
            bot_member = interaction.guild.me
            if bot_member and bot_member.top_role.position > 1:
                target_position = bot_member.top_role.position - 1
                if selected_role.position < target_position:
                    try:
                        await selected_role.edit(position=target_position, reason="Color role priority")
                    except Exception as e:
                        move_warning = f"تعذر رفع رتبة اللون تلقائيًا: {e}"

            # Remove any existing manageable colored role so selected color is applied immediately.
            remove_roles = []
            not_manageable_colored_roles = []
            for r in member.roles:
                if r.is_default() or r.id == selected_role_id:
                    continue
                if r.colour.value == 0:
                    continue

                # Bot can only manage roles lower than its top role.
                if bot_member and r.position < bot_member.top_role.position and not r.managed:
                    remove_roles.append(r)
                else:
                    not_manageable_colored_roles.append(r)

            try:
                if remove_roles:
                    await member.remove_roles(*remove_roles, reason="Switch color role")
                if selected_role not in member.roles:
                    await member.add_roles(selected_role, reason="Pick color role")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "لا أملك صلاحية تعديل الرتب. تأكد أن رتبة البوت أعلى من رتب الألوان.",
                    ephemeral=True,
                )
                return
            except Exception as e:
                await interaction.response.send_message(f"فشل تغيير اللون: {e}", ephemeral=True)
                return

            # Re-fetch member and verify effective color role shown in Discord.
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except Exception:
                member = interaction.guild.get_member(interaction.user.id) or member

            effective_color_role = None
            for r in sorted(member.roles, key=lambda x: x.position, reverse=True):
                if r.colour.value != 0:
                    effective_color_role = r
                    break

            if effective_color_role and effective_color_role.id != selected_role_id:
                note = (
                    f"تمت إضافة اللون {selected_label} لكن اللون الظاهر يتحكم فيه رول أعلى: "
                    f"**{effective_color_role.name}**."
                )
                if not_manageable_colored_roles:
                    locked = ", ".join(r.name for r in not_manageable_colored_roles[:3])
                    note += f"\nرولات ألوان أعلى لا أقدر أزيلها: {locked}"
                if move_warning:
                    note += f"\n{move_warning}"
                await interaction.response.send_message(note, ephemeral=True)
                return

            done_msg = f"تم تغيير لونك إلى: {selected_label}"
            if move_warning:
                done_msg += f"\n{move_warning}"
            await interaction.response.send_message(done_msg, ephemeral=True)

        btn.callback = on_click
        view.add_item(btn)

    clear_btn = discord.ui.Button(
        style=discord.ButtonStyle.danger,
        emoji="🚫",
        label="إزالة اللون",
        custom_id=f"pick-color-clear:{guild_id}",
        row=max(0, (len(role_entries) - 1) // 5 + 1),
    )

    async def on_clear(interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != guild_id:
            await interaction.response.send_message("هذه اللوحة لا تخص هذا السيرفر.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("لم أستطع العثور على العضو.", ephemeral=True)
            return

        bot_member = interaction.guild.me
        remove_roles = []
        blocked_roles = []
        for r in member.roles:
            if r.is_default() or r.colour.value == 0:
                continue
            if bot_member and r.position < bot_member.top_role.position and not r.managed:
                remove_roles.append(r)
            else:
                blocked_roles.append(r)

        if not remove_roles:
            if blocked_roles:
                names = ", ".join(r.name for r in blocked_roles[:3])
                await interaction.response.send_message(
                    f"لا أقدر إزالة الألوان لأن في رتب أعلى من البوت: {names}",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message("ما عندك لون مضاف حالياً.", ephemeral=True)
            return

        try:
            await member.remove_roles(*remove_roles, reason="Clear color role")
        except discord.Forbidden:
            await interaction.response.send_message(
                "لا أملك صلاحية تعديل الرتب. تأكد أن رتبة البوت أعلى من رتب الألوان.",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.response.send_message(f"فشل إزالة اللون: {e}", ephemeral=True)
            return

        await interaction.response.send_message("تمت إزالة اللون بنجاح.", ephemeral=True)

    clear_btn.callback = on_clear
    view.add_item(clear_btn)

    return view


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = (attachment.filename or "").lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


class ReminderBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        self.reminder_loop.start()
        await self.tree.sync()

    async def on_ready(self) -> None:
        logging.info(f"Bot logged in as {self.user} ({self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guild(s)")
        logging.info("Ready to accept /panel commands")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if is_server_registered(guild.id):
            try:
                sync_registered_server_channels(guild)
            except Exception as e:
                logging.warning(f"Failed to sync channels on guild join ({guild.id}): {e}")

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = getattr(channel, "guild", None)
        if guild and is_server_registered(guild.id):
            try:
                sync_registered_server_channels(guild)
            except Exception as e:
                logging.warning(f"Failed to sync channels on create ({guild.id}): {e}")

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = getattr(channel, "guild", None)
        if guild and is_server_registered(guild.id):
            try:
                sync_registered_server_channels(guild)
            except Exception as e:
                logging.warning(f"Failed to sync channels on delete ({guild.id}): {e}")

    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        guild = getattr(after, "guild", None)
        if guild and is_server_registered(guild.id):
            try:
                sync_registered_server_channels(guild)
            except Exception as e:
                logging.warning(f"Failed to sync channels on update ({guild.id}): {e}")

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def reminder_loop(self) -> None:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        current_marker = now.strftime("%Y-%m-%d %H:%M")

        conn = get_conn()
        try:
            rows = conn.execute("SELECT * FROM events").fetchall()
            for row in rows:
                try:
                    event_hour, event_minute, add_one_day = parse_event_time(str(row["time"]))
                except Exception:
                    continue

                remind_before = int(row["remind_before_minutes"] or 10)

                days_raw = str(row["days"] or "")

                def date_is_active(target_date: dt.date) -> bool:
                    if days_raw == "alt":
                        return is_every_other_day_active(row["created_at"], target_date)
                    day_tokens = {int(d) for d in days_raw.split(",") if d.isdigit()}
                    return target_date.weekday() in day_tokens

                should_send = False
                for offset_days in (0, 1):
                    event_date = now.date() + dt.timedelta(days=offset_days)
                    if not date_is_active(event_date):
                        continue

                    if add_one_day:
                        event_date = event_date + dt.timedelta(days=1)

                    event_dt = dt.datetime.combine(event_date, dt.time(hour=event_hour, minute=event_minute))
                    trigger_dt = event_dt - dt.timedelta(minutes=remind_before)
                    if trigger_dt == now:
                        should_send = True
                        break

                if not should_send:
                    continue

                if row["last_sent_marker"] == current_marker:
                    continue

                await self.send_event_reminder(row)
                conn.execute(
                    "UPDATE events SET last_sent_marker = ? WHERE id = ?",
                    (current_marker, row["id"]),
                )
            conn.commit()
        finally:
            conn.close()

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.wait_until_ready()

    async def send_event_reminder(self, row: sqlite3.Row) -> None:
        guild = self.get_guild(row["guild_id"])
        if not guild:
            logging.warning(f"Guild {row['guild_id']} not found")
            return

        lang = get_user_lang(row["creator_id"])
        msg_dict = MESSAGES.get(lang, MESSAGES["en"])

        embed = discord.Embed(
            title=msg_dict["event_reminder_title"],
            color=discord.Color.blurple(),
        )
        embed.add_field(name=msg_dict["event_field_name"], value=row["title"], inline=False)
        if row["message"]:
            embed.add_field(name="Message", value=row["message"], inline=False)
        if row["image_url"]:
            embed.set_image(url=row["image_url"])

        conn = get_conn()
        try:
            settings = conn.execute(
                "SELECT notification_channel_id FROM server_settings WHERE guild_id = ?",
                (row["guild_id"],),
            ).fetchone()
        finally:
            conn.close()

        mention = "@everyone"

        channel = None
        if row["channel_id"]:
            channel = guild.get_channel(int(row["channel_id"]))
        if channel is None and settings and settings["notification_channel_id"]:
            channel = guild.get_channel(settings["notification_channel_id"])
        if channel is None:
            channel = guild.text_channels[0] if guild.text_channels else None
        if channel:
            try:
                await channel.send(content=mention, embed=embed)
                logging.info(f"Reminder sent for event #{row['id']}")
            except Exception as e:
                logging.error(f"Failed to send reminder: {e}")


bot = ReminderBot()


class DaysSelectView(discord.ui.View):
    def __init__(self, callback, owner_id: int, include_alt_start: bool = False):
        super().__init__(timeout=300)
        self.callback = callback
        self.owner_id = owner_id
        self.include_alt_start = include_alt_start
        
        select = discord.ui.Select(
            placeholder="Select days",
            options=[
                discord.SelectOption(label="Every Day | كل الأيام", value="all"),
                discord.SelectOption(label="Every Other Day | يوم إيه / يوم لا", value="alt"),
                discord.SelectOption(label="Sunday | الأحد", value="6"),
                discord.SelectOption(label="Monday | الاثنين", value="0"),
                discord.SelectOption(label="Tuesday | الثلاثاء", value="1"),
                discord.SelectOption(label="Wednesday | الأربعاء", value="2"),
                discord.SelectOption(label="Thursday | الخميس", value="3"),
                discord.SelectOption(label="Friday | الجمعة", value="4"),
                discord.SelectOption(label="Saturday | السبت", value="5"),
            ],
            max_values=7,
            min_values=1,
        )
        select.callback = self.on_days_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_days_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return
        values = interaction.data["values"]
        if "all" in values:
            days = "0,1,2,3,4,5,6"
        elif "alt" in values:
            # If alt is selected, ask when to start the cycle
            if self.include_alt_start:
                await interaction.response.send_message(
                    "اختر بداية دورة يوم إيه / يوم لا:",
                    view=AltStartSelectView(self.callback, self.owner_id, "alt"),
                    ephemeral=True,
                )
                return
            days = "alt"
        else:
            days = ",".join(sorted(values, key=int))
        await self.callback(interaction, days)


class AltStartSelectView(discord.ui.View):
    def __init__(self, callback, owner_id: int, days_value: str):
        super().__init__(timeout=300)
        self.callback = callback
        self.owner_id = owner_id
        self.days = days_value

    @discord.ui.button(label="ابدأ من اليوم | Start Today", style=discord.ButtonStyle.primary)
    async def start_today(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return
        await self.callback(interaction, self.days)

    @discord.ui.button(label="ابدأ من الغد | Start Tomorrow", style=discord.ButtonStyle.secondary)
    async def start_tomorrow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return
        # Pass special marker to indicate starting from tomorrow
        await self.callback(interaction, f"{self.days}!tomorrow")


class ReminderMinutesSelectView(discord.ui.View):
    def __init__(self, callback, owner_id: int):
        super().__init__(timeout=300)
        self.callback = callback
        self.owner_id = owner_id

        select = discord.ui.Select(
            placeholder="Reminder before event (minutes)",
            options=[
                discord.SelectOption(label=f"{m} minutes", value=str(m))
                for m in REMINDER_MINUTES_OPTIONS
            ],
            max_values=1,
            min_values=1,
        )
        select.callback = self.on_minutes_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_minutes_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return
        minutes = int(interaction.data["values"][0])
        await self.callback(interaction, minutes)


class CreateEventModal(discord.ui.Modal, title="Create Event | إنشاء الفعالية"):
    def __init__(self):
        super().__init__(timeout=300)
        self.title_input = discord.ui.TextInput(label="Title | العنوان", max_length=120)
        self.time_input = discord.ui.TextInput(
            label="Event time | وقت الفعالية",
            placeholder="00:00, 00:30 ... 23:30, 24:00",
            max_length=5,
        )
        self.add_item(self.title_input)
        self.add_item(self.time_input)
        self.selected_time = None
        self.selected_remind_before = None
        self.selected_days = None
        self.selected_image_url = None
        self.selected_channel_id = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        selected_time = self.time_input.value.strip()
        if selected_time not in TIMES:
            await interaction.response.send_message(
                t(interaction.user.id, "invalid_time_choice"),
                ephemeral=True,
            )
            return

        self.selected_time = selected_time
        self.selected_image_url = None
        modal_self = self  # capture modal reference for inner class closures

        class ConfirmCreateView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)

            @discord.ui.button(label="Confirm | تأكيد", style=discord.ButtonStyle.success)
            async def confirm(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
                if inter.user.id != interaction.user.id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return
                await finalize_create(inter)

            @discord.ui.button(label="Cancel | إلغاء", style=discord.ButtonStyle.danger)
            async def cancel(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
                if inter.user.id != interaction.user.id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return
                await inter.response.edit_message(
                    content="تم إلغاء إنشاء التذكير.",
                    view=None,
                )

            @discord.ui.button(label="Upload Image | رفع صورة", style=discord.ButtonStyle.secondary)
            async def upload_image(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
                if inter.user.id != interaction.user.id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return

                if not interaction.channel:
                    await inter.response.send_message("لا يمكن رفع صورة هنا.", ephemeral=True)
                    return

                await inter.response.send_message(
                    "أرسل الصورة الآن كمرفق في نفس القناة خلال 60 ثانية.",
                    ephemeral=True,
                )

                def check(msg: discord.Message) -> bool:
                    return (
                        msg.author.id == interaction.user.id
                        and msg.channel.id == interaction.channel.id
                        and len(msg.attachments) > 0
                    )

                try:
                    msg = await bot.wait_for("message", timeout=60, check=check)
                except asyncio.TimeoutError:
                    await inter.followup.send("انتهى الوقت. أعد المحاولة.", ephemeral=True)
                    return

                attachment = msg.attachments[0]
                if not is_image_attachment(attachment):
                    await inter.followup.send("المرفق ليس صورة. أرسل صورة فقط.", ephemeral=True)
                    return

                modal_self.selected_image_url = attachment.url
                await inter.followup.send("تم حفظ الصورة بنجاح.", ephemeral=True)

        async def on_reminder_selected(inter: discord.Interaction, minutes: int) -> None:
            self.selected_remind_before = minutes
            await show_channel_selector(inter)

        async def on_days_selected(inter: discord.Interaction, days: str) -> None:
            self.selected_days = days
            await show_reminder_selector(inter)

        async def show_reminder_selector(inter: discord.Interaction) -> None:
            await inter.response.edit_message(
                content="Select reminder lead time (minutes before event):",
                view=ReminderMinutesSelectView(on_reminder_selected, interaction.user.id),
            )

        async def show_channel_selector(inter: discord.Interaction) -> None:
            if not interaction.guild:
                await show_summary(inter)
                return

            text_channels = sorted(interaction.guild.text_channels, key=lambda c: c.position)
            modal_state = self

            class EventChannelSelect(discord.ui.Select):
                def __init__(self):
                    options = [
                        discord.SelectOption(
                            label="Use server default channel | استخدم القناة الافتراضية",
                            value="default",
                        )
                    ]
                    options.extend(
                        discord.SelectOption(label=f"#{ch.name}"[:100], value=str(ch.id))
                        for ch in text_channels[:24]
                    )
                    super().__init__(
                        placeholder="Select channel for this reminder | اختر قناة هذا التذكير",
                        options=options,
                        min_values=1,
                        max_values=1,
                    )

                async def callback(self, select_inter: discord.Interaction) -> None:
                    if select_inter.user.id != interaction.user.id:
                        await select_inter.response.send_message("Not for you.", ephemeral=True)
                        return

                    selected = self.values[0]
                    self.view.selected_channel_id = None if selected == "default" else int(selected)
                    modal_state.selected_channel_id = self.view.selected_channel_id
                    await show_summary(select_inter)

            class EventChannelSelectView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=300)
                    self.selected_channel_id = None
                    self.add_item(EventChannelSelect())

            await inter.response.edit_message(
                content="اختر قناة هذا التذكير (أو اتركها على القناة الافتراضية):",
                view=EventChannelSelectView(),
            )

        async def show_summary(inter: discord.Interaction) -> None:
            image_status = self.selected_image_url if self.selected_image_url else "No image"
            channel_status = (
                f"<#{self.selected_channel_id}>" if self.selected_channel_id else "Default server channel"
            )
            summary = (
                "Summary | الملخص\n"
                f"- Title | العنوان: {self.title_input.value.strip()}\n"
                f"- Event Time | وقت الفعالية: {self.selected_time}\n"
                f"- Days | الأيام: {format_days_summary(self.selected_days)}\n"
                f"- Reminder Before | التذكير قبل: {self.selected_remind_before} دقيقة\n"
                f"- Channel | القناة: {channel_status}\n"
                f"- Image | الصورة: {image_status}"
            )
            await inter.response.edit_message(
                content=summary,
                view=ConfirmCreateView(),
            )

        async def finalize_create(inter: discord.Interaction) -> None:
            # Handle the alt start date marker
            created_at_value = None
            clean_days = self.selected_days
            
            if "!tomorrow" in self.selected_days:
                # User chose to start from tomorrow
                tomorrow = dt.datetime.now().date() + dt.timedelta(days=1)
                created_at_value = dt.datetime.combine(tomorrow, dt.time()).isoformat()
                clean_days = self.selected_days.replace("!tomorrow", "")
            
            conn = get_conn()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO events (
                        guild_id, creator_id, title, time, days,
                        remind_before_minutes, message, image_url, channel_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        interaction.guild.id,
                        interaction.user.id,
                        self.title_input.value.strip(),
                        self.selected_time,
                        clean_days,
                        int(self.selected_remind_before),
                        None,
                        self.selected_image_url,
                        self.selected_channel_id,
                        created_at_value or dt.datetime.now().isoformat(),
                    ),
                )
                conn.commit()
                event_id = cursor.lastrowid
            finally:
                conn.close()

            await inter.response.edit_message(
                content=t(interaction.user.id, "event_created", event_id=event_id),
                view=None,
            )

        await interaction.response.send_message(
            content="Select days for the reminder:",
            view=DaysSelectView(on_days_selected, interaction.user.id, include_alt_start=True),
            ephemeral=True,
        )


class EditMessageModal(discord.ui.Modal, title="Edit Reminder Message"):
    def __init__(self, event_id: int, current_message: Optional[str] = None):
        super().__init__(timeout=300)
        self.event_id = event_id
        self.message_input = discord.ui.TextInput(
            label="Reminder Message",
            style=discord.TextStyle.paragraph,
            required=False,
            default=current_message or "",
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            conn.execute(
                """
                UPDATE events SET message = ?
                WHERE id = ? AND creator_id = ?
                """,
                (
                    self.message_input.value.strip() or None,
                    self.event_id,
                    interaction.user.id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        await interaction.response.send_message(
            t(interaction.user.id, "event_updated"),
            ephemeral=True,
        )


class EditScheduleModal(discord.ui.Modal, title="Edit Reminder Schedule"):
    def __init__(
        self,
        event_id: int,
        current_title: str,
        current_time: str,
        current_days: str,
        current_remind_before: int,
    ):
        super().__init__(timeout=300)
        self.event_id = event_id
        self.current_days = current_days
        self.title_input = discord.ui.TextInput(
            label="Title | العنوان",
            max_length=120,
            default=current_title,
        )
        self.time_input = discord.ui.TextInput(
            label="Event time | وقت الفعالية",
            placeholder="00:00, 00:30 ... 23:30, 24:00",
            max_length=5,
            default=current_time,
        )
        self.remind_before_input = discord.ui.TextInput(
            label="Reminder before (minutes)",
            placeholder="5",
            max_length=4,
            default=str(current_remind_before),
        )
        self.add_item(self.title_input)
        self.add_item(self.time_input)
        self.add_item(self.remind_before_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        title = self.title_input.value.strip()
        time_value = self.time_input.value.strip()
        remind_before_raw = self.remind_before_input.value.strip()

        if not title:
            await interaction.response.send_message("العنوان مطلوب.", ephemeral=True)
            return

        if time_value not in TIMES:
            await interaction.response.send_message(
                t(interaction.user.id, "invalid_time_choice"),
                ephemeral=True,
            )
            return

        if not remind_before_raw.isdigit():
            await interaction.response.send_message("قيمة التذكير يجب أن تكون رقماً.", ephemeral=True)
            return

        remind_before = int(remind_before_raw)
        if remind_before < 1 or remind_before > 1440:
            await interaction.response.send_message("وقت التذكير يجب أن يكون بين 1 و 1440 دقيقة.", ephemeral=True)
            return

        async def on_days_selected(inter: discord.Interaction, days_csv: str) -> None:
            conn = get_conn()
            try:
                conn.execute(
                    """
                    UPDATE events
                    SET title = ?, time = ?, days = ?, remind_before_minutes = ?, last_sent_marker = NULL
                    WHERE id = ? AND creator_id = ?
                    """,
                    (
                        title,
                        time_value,
                        days_csv,
                        remind_before,
                        self.event_id,
                        inter.user.id,
                    ),
                )
                conn.commit()

                current_row = conn.execute(
                    "SELECT channel_id FROM events WHERE id = ? AND creator_id = ?",
                    (self.event_id, inter.user.id),
                ).fetchone()
                current_channel_id = current_row["channel_id"] if current_row else None
            finally:
                conn.close()

            if not inter.guild:
                await inter.response.edit_message(
                    content="تم تحديث الوقت والأيام وإعدادات التذكير بنجاح.",
                    view=None,
                )
                return

            text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)

            class ScheduleChannelSelect(discord.ui.Select):
                def __init__(self):
                    options = [
                        discord.SelectOption(
                            label="Keep current channel | نفس القناة الحالية",
                            value="keep",
                        ),
                        discord.SelectOption(
                            label="Use server default channel | القناة الافتراضية",
                            value="default",
                        ),
                    ]
                    options.extend(
                        discord.SelectOption(label=f"#{ch.name}"[:100], value=str(ch.id))
                        for ch in text_channels[:23]
                    )
                    super().__init__(
                        placeholder="اختياري: اختر قناة هذا التذكير",
                        options=options,
                        min_values=1,
                        max_values=1,
                    )

                async def callback(self, select_inter: discord.Interaction) -> None:
                    if select_inter.user.id != inter.user.id:
                        await select_inter.response.send_message("Not for you.", ephemeral=True)
                        return

                    selected = self.values[0]
                    if selected == "keep":
                        new_channel_id = current_channel_id
                    elif selected == "default":
                        new_channel_id = None
                    else:
                        new_channel_id = int(selected)

                    conn2 = get_conn()
                    try:
                        conn2.execute(
                            "UPDATE events SET channel_id = ? WHERE id = ? AND creator_id = ?",
                            (new_channel_id, self.view.event_id, select_inter.user.id),
                        )
                        conn2.commit()
                    finally:
                        conn2.close()

                    channel_text = f"<#{new_channel_id}>" if new_channel_id else "Default"
                    await select_inter.response.edit_message(
                        content=(
                            "تم تحديث الوقت والأيام وإعدادات التذكير بنجاح.\n"
                            f"Channel: {channel_text}"
                        ),
                        view=None,
                    )

            class ScheduleChannelSelectView(discord.ui.View):
                def __init__(self, event_id: int):
                    super().__init__(timeout=300)
                    self.event_id = event_id
                    self.add_item(ScheduleChannelSelect())

            await inter.response.edit_message(
                content=(
                    "تم تحديث الوقت والأيام بنجاح.\n"
                    "اختياريًا: اختر القناة لهذا التذكير أو اتركها كما هي."
                ),
                view=ScheduleChannelSelectView(self.event_id),
            )

        await interaction.response.send_message(
            "اختر الأيام الجديدة (تقدر تختار يوم واحد أو عدة أيام أو كل الأيام):",
            view=DaysSelectView(on_days_selected, interaction.user.id),
            ephemeral=True,
        )


class ControlPanelView(discord.ui.View):
    def __init__(self, owner_id: Optional[int] = None):
        super().__init__(timeout=600 if owner_id else None)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create Event | إنشاء", style=discord.ButtonStyle.primary)
    async def create_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CreateEventModal())

    @discord.ui.button(label="List Events | عرض", style=discord.ButtonStyle.secondary)
    async def list_events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE creator_id = ? AND guild_id = ? ORDER BY time ASC",
                (interaction.user.id, interaction.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message(t(interaction.user.id, "no_events"), ephemeral=True)
            return

        lines = [t(interaction.user.id, "events_header")]
        for row in rows[:20]:
            days_str = format_days_summary(str(row["days"] or ""))
            channel_str = f"<#{row['channel_id']}>" if row["channel_id"] else "Default"
            lines.append(
                f"• ID {row['id']} | {row['title']} | {row['time']} | -{row['remind_before_minutes']}m | {days_str} | {channel_str}"
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Edit Message | تعديل الرسالة", style=discord.ButtonStyle.success)
    async def edit_message(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id, title, time, days, remind_before_minutes, message, image_url, channel_id
                FROM events
                WHERE creator_id = ? AND guild_id = ?
                ORDER BY time ASC
                """,
                (interaction.user.id, interaction.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message(t(interaction.user.id, "no_events"), ephemeral=True)
            return

        class EventActionView(discord.ui.View):
            def __init__(self, owner_id: int, event_row: sqlite3.Row, event_rows):
                super().__init__(timeout=300)
                self.owner_id = owner_id
                self.event_row = event_row
                self.event_rows = event_rows

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != self.owner_id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="Edit Time | تعديل الوقت", style=discord.ButtonStyle.primary)
            async def edit_time_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(
                    EditScheduleModal(
                        event_id=self.event_row["id"],
                        current_title=self.event_row["title"],
                        current_time=self.event_row["time"],
                        current_days=self.event_row["days"],
                        current_remind_before=int(self.event_row["remind_before_minutes"]),
                    )
                )

            @discord.ui.button(label="Edit Message | تعديل الرسالة", style=discord.ButtonStyle.success)
            async def edit_content_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(
                    EditMessageModal(
                        self.event_row["id"],
                        self.event_row["message"],
                    )
                )

            @discord.ui.button(label="Upload Image | رفع صورة", style=discord.ButtonStyle.secondary)
            async def upload_image_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_message(
                    "أرسل الصورة الآن كمرفق في نفس القناة خلال 60 ثانية.",
                    ephemeral=True,
                )

                if not inter.channel:
                    await inter.followup.send("لا يمكن رفع صورة هنا.", ephemeral=True)
                    return

                def check(msg: discord.Message) -> bool:
                    return (
                        msg.author.id == self.owner_id
                        and msg.channel.id == inter.channel.id
                        and len(msg.attachments) > 0
                    )

                try:
                    msg = await bot.wait_for("message", timeout=60, check=check)
                except asyncio.TimeoutError:
                    await inter.followup.send("انتهى الوقت. أعد المحاولة.", ephemeral=True)
                    return

                attachment = msg.attachments[0]
                if not is_image_attachment(attachment):
                    await inter.followup.send("المرفق ليس صورة. أرسل صورة فقط.", ephemeral=True)
                    return

                conn2 = get_conn()
                try:
                    conn2.execute(
                        "UPDATE events SET image_url = ? WHERE id = ? AND creator_id = ?",
                        (attachment.url, self.event_row["id"], self.owner_id),
                    )
                    conn2.commit()
                finally:
                    conn2.close()

                await inter.followup.send("تم تحديث الصورة بنجاح.", ephemeral=True)

            @discord.ui.button(label="Edit Channel | تعديل القناة", style=discord.ButtonStyle.primary)
            async def edit_channel_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild:
                    await inter.response.send_message("Server only.", ephemeral=True)
                    return

                text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)

                class EventChannelSelect(discord.ui.Select):
                    def __init__(self):
                        options = [
                            discord.SelectOption(
                                label="Use server default channel | استخدم القناة الافتراضية",
                                value="default",
                            )
                        ]
                        options.extend(
                            discord.SelectOption(label=f"#{ch.name}"[:100], value=str(ch.id))
                            for ch in text_channels[:24]
                        )
                        super().__init__(
                            placeholder="Select new channel for this reminder",
                            options=options,
                            min_values=1,
                            max_values=1,
                        )

                    async def callback(self, select_inter: discord.Interaction) -> None:
                        if select_inter.user.id != self.view.owner_id:
                            await select_inter.response.send_message("Not for you.", ephemeral=True)
                            return

                        selected = self.values[0]
                        new_channel_id = None if selected == "default" else int(selected)

                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET channel_id = ? WHERE id = ? AND creator_id = ?",
                                (new_channel_id, self.view.event_id, self.view.owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()

                        channel_label = f"<#{new_channel_id}>" if new_channel_id else "Default"
                        await select_inter.response.edit_message(
                            content=f"تم تحديث قناة التذكير بنجاح.\nChannel: {channel_label}",
                            view=None,
                        )

                class EventChannelSelectView(discord.ui.View):
                    def __init__(self, owner_id: int, event_id: int):
                        super().__init__(timeout=300)
                        self.owner_id = owner_id
                        self.event_id = event_id
                        self.add_item(EventChannelSelect())

                await inter.response.edit_message(
                    content="اختر القناة الجديدة لهذا التذكير (أو القناة الافتراضية):",
                    view=EventChannelSelectView(self.owner_id, self.event_row["id"]),
                )

            @discord.ui.button(label="Delete | حذف", style=discord.ButtonStyle.danger)
            async def delete_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                conn2 = get_conn()
                try:
                    conn2.execute(
                        "DELETE FROM events WHERE id = ? AND creator_id = ?",
                        (self.event_row["id"], self.owner_id),
                    )
                    conn2.commit()
                finally:
                    conn2.close()

                await inter.response.edit_message(
                    content=t(self.owner_id, "event_deleted"),
                    view=None,
                )

            @discord.ui.button(label="Back | رجوع", style=discord.ButtonStyle.secondary)
            async def back_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.edit_message(
                    content="اختر التذكير الذي تريد التعديل أو الحذف عليه:",
                    view=EventPickerView(self.owner_id, self.event_rows),
                )

        class EventPickerView(discord.ui.View):
            def __init__(self, owner_id: int, event_rows):
                super().__init__(timeout=300)
                self.owner_id = owner_id

                for row in event_rows[:25]:
                    label = f"{row['title'][:35]} | {row['time']}"
                    btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

                    async def on_pick(inter: discord.Interaction, selected=row) -> None:
                        if inter.user.id != self.owner_id:
                            await inter.response.send_message("Not for you.", ephemeral=True)
                            return

                        days_str = format_days_summary(selected["days"])
                        channel_str = f"<#{selected['channel_id']}>" if selected["channel_id"] else "Default"
                        summary = (
                            f"Event #{selected['id']}\n"
                            f"Title: {selected['title']}\n"
                            f"Time: {selected['time']}\n"
                            f"Days: {days_str}\n"
                            f"Reminder: -{selected['remind_before_minutes']}m\n"
                            f"Channel: {channel_str}"
                        )
                        await inter.response.edit_message(
                            content=summary,
                            view=EventActionView(self.owner_id, selected, event_rows),
                        )

                    btn.callback = on_pick
                    self.add_item(btn)

        await interaction.response.send_message(
            "اختر التذكير الذي تريد التعديل أو الحذف عليه:",
            view=EventPickerView(interaction.user.id, rows),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete | حذف", style=discord.ButtonStyle.danger)
    async def delete_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, title FROM events WHERE creator_id = ? AND guild_id = ?",
                (interaction.user.id, interaction.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message(t(interaction.user.id, "no_events"), ephemeral=True)
            return

        class DeleteView(discord.ui.View):
            def __init__(self, parent_user_id: int):
                super().__init__(timeout=300)
                self.parent_user_id = parent_user_id

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != self.parent_user_id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return False
                return True

            def _build_buttons(self, rows_data) -> None:
                for row in rows_data[:25]:
                    def make_delete(event_id: int):
                        async def delete_callback(inter: discord.Interaction) -> None:
                            conn2 = get_conn()
                            try:
                                conn2.execute(
                                    "DELETE FROM events WHERE id = ? AND creator_id = ?",
                                    (event_id, self.parent_user_id),
                                )
                                conn2.commit()
                            finally:
                                conn2.close()
                            await inter.response.send_message(
                                t(self.parent_user_id, "event_deleted"),
                                ephemeral=True,
                            )
                        return delete_callback

                    btn = discord.ui.Button(
                        label=f"🗑 {row['title'][:22]}",
                        style=discord.ButtonStyle.danger,
                    )
                    btn.callback = make_delete(row["id"])
                    self.add_item(btn)

        dv = DeleteView(interaction.user.id)
        dv._build_buttons(rows)

        await interaction.response.send_message(
            "اختر التذكير الذي تريد حذفه:",
            view=dv,
            ephemeral=True,
        )

    @discord.ui.button(label="Language | اللغة", style=discord.ButtonStyle.secondary)
    async def language(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        class LanguageView(discord.ui.View):
            def __init__(self, parent_user_id: int):
                super().__init__(timeout=180)
                self.parent_user_id = parent_user_id

            @discord.ui.button(label="English", style=discord.ButtonStyle.secondary)
            async def english(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                set_user_lang(self.parent_user_id, "en")
                await inter.response.send_message(t(self.parent_user_id, "lang_set"), ephemeral=True)

            @discord.ui.button(label="العربية", style=discord.ButtonStyle.secondary)
            async def arabic(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                set_user_lang(self.parent_user_id, "ar")
                await inter.response.send_message(t(self.parent_user_id, "lang_set"), ephemeral=True)

        await interaction.response.send_message(
            "Choose language:",
            view=LanguageView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Settings | الإعدادات", style=discord.ButtonStyle.secondary)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        class SettingsView(discord.ui.View):
            def __init__(self, parent_user_id: int, parent_guild_id: int):
                super().__init__(timeout=300)
                self.parent_user_id = parent_user_id
                self.parent_guild_id = parent_guild_id

            @discord.ui.button(label="View Registered | عرض المسجل", style=discord.ButtonStyle.secondary)
            async def view_registered(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
                    await inter.response.send_message("Admins only. | للمشرفين فقط.", ephemeral=True)
                    return

                conn = get_conn()
                try:
                    rows = conn.execute(
                        "SELECT guild_id, notification_channel_id FROM server_settings ORDER BY guild_id ASC"
                    ).fetchall()
                finally:
                    conn.close()

                if not rows:
                    await inter.response.send_message("لا يوجد أي سيرفر مسجل حالياً.", ephemeral=True)
                    return

                lines = ["السيرفرات المسجلة:"]
                for row in rows[:20]:
                    gid = row["guild_id"]
                    channel_text = f"<#{row['notification_channel_id']}>" if row["notification_channel_id"] else "غير محددة"
                    marker = " (هذا السيرفر)" if gid == self.parent_guild_id else ""
                    lines.append(f"- {gid}{marker} | قناة التذكير: {channel_text}")

                if len(rows) > 20:
                    lines.append(f"... +{len(rows) - 20} more")

                await inter.response.send_message("\n".join(lines), ephemeral=True)

            @discord.ui.button(label="Add Admin | إضافة مشرف", style=discord.ButtonStyle.secondary)
            async def add_admin(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
                    await inter.response.send_message("Admins only. | للمشرفين فقط.", ephemeral=True)
                    return

                class AdminModal(discord.ui.Modal, title="Add Admin"):
                    def __init__(self, parent_user_id: int, parent_guild_id: int):
                        super().__init__()
                        self.parent_user_id = parent_user_id
                        self.parent_guild_id = parent_guild_id
                        self.user_input = discord.ui.TextInput(label="User ID or mention", placeholder="123456789 or @user")
                        self.add_item(self.user_input)

                    async def on_submit(self, modal_inter: discord.Interaction) -> None:
                        user_text = self.user_input.value.strip()
                        user_id = None
                        if user_text.isdigit():
                            user_id = int(user_text)
                        elif user_text.startswith("<@") and user_text.endswith(">"):
                            try:
                                user_id = int(user_text[2:-1])
                            except:
                                pass

                        if not user_id:
                            await modal_inter.response.send_message("Invalid user.", ephemeral=True)
                            return

                        conn = get_conn()
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO admins (guild_id, user_id) VALUES (?, ?)",
                                (self.parent_guild_id, user_id),
                            )
                            conn.commit()
                        finally:
                            conn.close()

                        await modal_inter.response.send_message(t(self.parent_user_id, "admin_added"), ephemeral=True)

                await inter.response.send_modal(AdminModal(self.parent_user_id, self.parent_guild_id))

            @discord.ui.button(label="Owner Tools | أدوات المالك", style=discord.ButtonStyle.danger)
            async def owner_tools(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not is_bot_owner(inter.user.id):
                    await inter.response.send_message("هذه الأدوات خاصة بمالك البوت فقط.", ephemeral=True)
                    return

                class OwnerToolsView(discord.ui.View):
                    def __init__(self, guild_id: int):
                        super().__init__(timeout=300)
                        self.guild_id = guild_id

                    @discord.ui.button(label="Upgrade Bot | ترقية البوت", style=discord.ButtonStyle.success)
                    async def upgrade_bot_owner(self, owner_inter: discord.Interaction, owner_btn: discord.ui.Button) -> None:
                        await owner_inter.response.defer(ephemeral=True, thinking=True)

                        bot_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
                        git_proc = await asyncio.create_subprocess_exec(
                            "git", "pull",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=bot_dir,
                        )
                        git_out, git_err = await git_proc.communicate()
                        git_text = (git_out.decode().strip() or git_err.decode().strip())[:700]
                        if git_proc.returncode != 0:
                            await owner_inter.followup.send(f"Git pull failed:\n```\n{git_text}\n```", ephemeral=True)
                            return

                        pip_proc = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "pip",
                            "install",
                            "-r",
                            "requirements.txt",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=bot_dir,
                        )
                        pip_out, pip_err = await pip_proc.communicate()
                        pip_text = (pip_out.decode().strip() or pip_err.decode().strip())[-700:]
                        if pip_proc.returncode != 0:
                            await owner_inter.followup.send(f"Pip install failed:\n```\n{pip_text}\n```", ephemeral=True)
                            return

                        await owner_inter.followup.send(
                            f"تمت ترقية البوت بنجاح.\nGit:\n```\n{git_text}\n```\nPip (آخر سطور):\n```\n{pip_text}\n```\nسيتم إعادة التشغيل الآن...",
                            ephemeral=True,
                        )

                        async def _restart() -> None:
                            await asyncio.sleep(2)
                            os.execv(sys.executable, [sys.executable] + sys.argv)

                        asyncio.create_task(_restart())

                    @discord.ui.button(label="Add Bot Admin | إضافة أدمن", style=discord.ButtonStyle.primary)
                    async def add_bot_admin(self, owner_inter: discord.Interaction, owner_btn: discord.ui.Button) -> None:
                        class OwnerAddAdminModal(discord.ui.Modal, title="Owner: Add Bot Admin"):
                            def __init__(self, guild_id: int):
                                super().__init__(timeout=300)
                                self.guild_id = guild_id
                                self.user_input = discord.ui.TextInput(
                                    label="User ID",
                                    placeholder="123456789012345678",
                                    max_length=25,
                                )
                                self.add_item(self.user_input)

                            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                                raw = self.user_input.value.strip()
                                if not raw.isdigit():
                                    await modal_inter.response.send_message("User ID غير صحيح.", ephemeral=True)
                                    return

                                user_id = int(raw)
                                conn = get_conn()
                                try:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO admins (guild_id, user_id) VALUES (?, ?)",
                                        (self.guild_id, user_id),
                                    )
                                    conn.commit()
                                finally:
                                    conn.close()

                                await modal_inter.response.send_message(
                                    f"تمت إضافة المستخدم `{user_id}` كأدمن للبوت في هذا السيرفر.",
                                    ephemeral=True,
                                )

                        await owner_inter.response.send_modal(OwnerAddAdminModal(self.guild_id))

                    @discord.ui.button(label="Remove Bot Admin | حذف أدمن", style=discord.ButtonStyle.secondary)
                    async def remove_bot_admin(self, owner_inter: discord.Interaction, owner_btn: discord.ui.Button) -> None:
                        class OwnerRemoveAdminModal(discord.ui.Modal, title="Owner: Remove Bot Admin"):
                            def __init__(self, guild_id: int):
                                super().__init__(timeout=300)
                                self.guild_id = guild_id
                                self.user_input = discord.ui.TextInput(
                                    label="User ID",
                                    placeholder="123456789012345678",
                                    max_length=25,
                                )
                                self.add_item(self.user_input)

                            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                                raw = self.user_input.value.strip()
                                if not raw.isdigit():
                                    await modal_inter.response.send_message("User ID غير صحيح.", ephemeral=True)
                                    return

                                user_id = int(raw)
                                conn = get_conn()
                                try:
                                    conn.execute(
                                        "DELETE FROM admins WHERE guild_id = ? AND user_id = ?",
                                        (self.guild_id, user_id),
                                    )
                                    conn.commit()
                                finally:
                                    conn.close()

                                await modal_inter.response.send_message(
                                    f"تم حذف المستخدم `{user_id}` من أدمن البوت في هذا السيرفر.",
                                    ephemeral=True,
                                )

                        await owner_inter.response.send_modal(OwnerRemoveAdminModal(self.guild_id))

                    @discord.ui.button(label="List Bot Admins | عرض الأدمن", style=discord.ButtonStyle.secondary)
                    async def list_bot_admins(self, owner_inter: discord.Interaction, owner_btn: discord.ui.Button) -> None:
                        conn = get_conn()
                        try:
                            rows = conn.execute(
                                "SELECT user_id FROM admins WHERE guild_id = ? ORDER BY user_id ASC",
                                (self.guild_id,),
                            ).fetchall()
                        finally:
                            conn.close()

                        if not rows:
                            await owner_inter.response.send_message("لا يوجد أدمن للبوت في هذا السيرفر.", ephemeral=True)
                            return

                        lines = ["Bot Admins:"]
                        lines.extend(f"- <@{row['user_id']}> (`{row['user_id']}`)" for row in rows[:30])
                        if len(rows) > 30:
                            lines.append(f"... +{len(rows) - 30} more")
                        await owner_inter.response.send_message("\n".join(lines), ephemeral=True)

                    @discord.ui.button(label="Sync Commands | مزامنة الأوامر", style=discord.ButtonStyle.primary)
                    async def sync_commands(self, owner_inter: discord.Interaction, owner_btn: discord.ui.Button) -> None:
                        await owner_inter.response.defer(ephemeral=True, thinking=True)
                        synced = await bot.tree.sync()
                        await owner_inter.followup.send(
                            f"تمت مزامنة الأوامر بنجاح. عدد الأوامر: {len(synced)}",
                            ephemeral=True,
                        )

                    @discord.ui.button(label="Add Link Button | إضافة زر جديد", style=discord.ButtonStyle.success)
                    async def add_link_button(self, owner_inter: discord.Interaction, owner_btn: discord.ui.Button) -> None:
                        class AddLinkButtonModal(discord.ui.Modal, title="Owner: Add Link Button"):
                            def __init__(self):
                                super().__init__(timeout=300)
                                self.message_input = discord.ui.TextInput(
                                    label="Message",
                                    placeholder="اكتب رسالة الزر",
                                    required=False,
                                    default="اضغط الزر:",
                                    max_length=200,
                                )
                                self.label_input = discord.ui.TextInput(
                                    label="Button Label",
                                    placeholder="مثال: موقعنا",
                                    max_length=80,
                                )
                                self.url_input = discord.ui.TextInput(
                                    label="Button URL",
                                    placeholder="https://example.com",
                                    max_length=200,
                                )
                                self.add_item(self.message_input)
                                self.add_item(self.label_input)
                                self.add_item(self.url_input)

                            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                                if not modal_inter.channel:
                                    await modal_inter.response.send_message("لا يمكن النشر في هذه القناة.", ephemeral=True)
                                    return

                                url = self.url_input.value.strip()
                                if not (url.startswith("http://") or url.startswith("https://")):
                                    await modal_inter.response.send_message("الرابط يجب أن يبدأ بـ http:// أو https://", ephemeral=True)
                                    return

                                link_view = discord.ui.View(timeout=None)
                                link_view.add_item(
                                    discord.ui.Button(
                                        label=self.label_input.value.strip(),
                                        style=discord.ButtonStyle.link,
                                        url=url,
                                    )
                                )

                                await modal_inter.channel.send(
                                    content=self.message_input.value.strip() or "اضغط الزر:",
                                    view=link_view,
                                )
                                await modal_inter.response.send_message("تم نشر الزر الجديد بنجاح.", ephemeral=True)

                        await owner_inter.response.send_modal(AddLinkButtonModal())

                await inter.response.send_message(
                    "لوحة المالك الخاصة:\n- ترقية البوت\n- إدارة أدمن البوت\n- مزامنة الأوامر\n- إضافة زر رابط جديد",
                    view=OwnerToolsView(self.parent_guild_id),
                    ephemeral=True,
                )

            @discord.ui.button(label="Register Server | تسجيل سيرفر", style=discord.ButtonStyle.primary)
            async def register_server(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
                    await inter.response.send_message("Admins only. | للمشرفين فقط.", ephemeral=True)
                    return

                class ServerIDModal(discord.ui.Modal, title="Register Server | تسجيل سيرفر"):
                    def __init__(self, parent_user_id: int):
                        super().__init__(timeout=300)
                        self.parent_user_id = parent_user_id
                        self.server_id_input = discord.ui.TextInput(
                            label="Server ID | آيدي السيرفر",
                            placeholder="123456789012345678",
                            max_length=25,
                        )
                        self.add_item(self.server_id_input)

                    async def on_submit(self, modal_inter: discord.Interaction) -> None:
                        raw = self.server_id_input.value.strip()
                        if not raw.isdigit():
                            await modal_inter.response.send_message(
                                "آيدي السيرفر يجب أن يكون رقماً فقط.",
                                ephemeral=True,
                            )
                            return

                        await modal_inter.response.defer(ephemeral=True, thinking=True)

                        guild_id = int(raw)

                        conn = get_conn()
                        try:
                            existing = conn.execute(
                                "SELECT notification_channel_id FROM server_settings WHERE guild_id = ?",
                                (guild_id,),
                            ).fetchone()
                        finally:
                            conn.close()

                        if existing:
                            channel_text = (
                                f"<#{existing['notification_channel_id']}>"
                                if existing["notification_channel_id"]
                                else "غير محددة"
                            )
                            await modal_inter.followup.send(
                                (
                                    "هذا السيرفر مسجل مسبقاً، لا يمكن تسجيله مرة ثانية.\n"
                                    f"Server ID: **{guild_id}**\n"
                                    f"Reminder Channel: {channel_text}"
                                ),
                                ephemeral=True,
                            )
                            return

                        try:
                            target_guild = await bot.fetch_guild(guild_id)
                            channels = await target_guild.fetch_channels()
                        except discord.NotFound:
                            await modal_inter.followup.send(
                                "السيرفر غير موجود أو الآيدي غير صحيح.",
                                ephemeral=True,
                            )
                            return
                        except discord.Forbidden:
                            await modal_inter.followup.send(
                                "البوت غير موجود داخل هذا السيرفر أو لا يملك الصلاحيات الكافية.",
                                ephemeral=True,
                            )
                            return
                        except Exception as e:
                            await modal_inter.followup.send(
                                f"فشل التسجيل بسبب خطأ: {e}",
                                ephemeral=True,
                            )
                            return

                        conn = get_conn()
                        try:
                            conn.execute(
                                """
                                INSERT INTO server_settings (guild_id)
                                VALUES (?)
                                ON CONFLICT(guild_id) DO NOTHING
                                """,
                                (guild_id,),
                            )
                            conn.commit()
                        finally:
                            conn.close()

                        text_channels = [c for c in channels if isinstance(c, discord.TextChannel)]
                        text_channels = sorted(text_channels, key=lambda c: c.position)

                        if not text_channels:
                            await modal_inter.followup.send(
                                (
                                    f"تم تسجيل السيرفر بنجاح.\n"
                                    f"Server: **{target_guild.name}** (`{target_guild.id}`)\n\n"
                                    "لا توجد رومات نصية في هذا السيرفر."
                                ),
                                ephemeral=True,
                            )
                            return

                        class ChannelSelect(discord.ui.Select):
                            def __init__(self):
                                options = [
                                    discord.SelectOption(
                                        label=f"#{ch.name}"[:100],
                                        value=str(ch.id),
                                        description=f"ID: {ch.id}"[:100],
                                    )
                                    for ch in text_channels[:25]
                                ]
                                super().__init__(
                                    placeholder="اختر روم التذكير | Select reminder channel",
                                    options=options,
                                    min_values=1,
                                    max_values=1,
                                )

                            async def callback(self, select_inter: discord.Interaction) -> None:
                                channel_id = int(self.values[0])
                                channel_name = next(
                                    (ch.name for ch in text_channels if ch.id == channel_id),
                                    str(channel_id),
                                )

                                conn2 = get_conn()
                                try:
                                    conn2.execute(
                                        """
                                        INSERT INTO server_settings (guild_id, notification_channel_id)
                                        VALUES (?, ?)
                                        ON CONFLICT(guild_id) DO UPDATE SET
                                            notification_channel_id = excluded.notification_channel_id
                                        """,
                                        (target_guild.id, channel_id),
                                    )
                                    conn2.commit()
                                finally:
                                    conn2.close()

                                await select_inter.response.edit_message(
                                    content=(
                                        "تم تسجيل السيرفر وتحديد روم التذكير بنجاح.\n"
                                        f"Server: **{target_guild.name}** (`{target_guild.id}`)\n"
                                        f"Reminder Channel: **#{channel_name}** (`{channel_id}`)"
                                    ),
                                    view=None,
                                )

                        class ChannelSelectView(discord.ui.View):
                            def __init__(self):
                                super().__init__(timeout=300)
                                self.add_item(ChannelSelect())

                        preview_lines = [f"- #{ch.name} (`{ch.id}`)" for ch in text_channels[:10]]
                        remaining = len(text_channels) - 10
                        if remaining > 0:
                            preview_lines.append(f"... +{remaining} more")

                        await modal_inter.followup.send(
                            (
                                f"تم تسجيل السيرفر بنجاح.\n"
                                f"Server: **{target_guild.name}** (`{target_guild.id}`)\n\n"
                                f"الرومات النصية المتاحة (مزامنة مباشرة):\n{chr(10).join(preview_lines)}\n\n"
                                "اختر روم التذكير من القائمة أدناه:"
                            ),
                            view=ChannelSelectView(),
                            ephemeral=True,
                        )

                await inter.response.send_modal(ServerIDModal(inter.user.id))

            @discord.ui.button(label="اختر لونك | Choose Color", style=discord.ButtonStyle.primary)
            async def color_picker_setup(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild:
                    await inter.response.send_message("Server only.", ephemeral=True)
                    return

                if not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
                    await inter.response.send_message("Admins only. | للمشرفين فقط.", ephemeral=True)
                    return

                class ColorChannelSelect(discord.ui.ChannelSelect):
                    async def callback(self, select_inter: discord.Interaction) -> None:
                        selected_ref = self.values[0] if self.values else None
                        if not selected_ref:
                            await select_inter.response.send_message("No channel selected.", ephemeral=True)
                            return

                        await select_inter.response.defer(ephemeral=True, thinking=True)

                        try:
                            role_entries = await ensure_color_roles(select_inter.guild)
                        except Exception as e:
                            await select_inter.followup.send(f"فشل إنشاء رتب الألوان: {e}", ephemeral=True)
                            return

                        channel_id = selected_ref.id
                        selected_channel = select_inter.guild.get_channel(channel_id)
                        if selected_channel is None:
                            try:
                                fetched = await select_inter.guild.fetch_channel(channel_id)
                                selected_channel = fetched if isinstance(fetched, discord.TextChannel) else None
                            except Exception:
                                selected_channel = None

                        if selected_channel is None:
                            await select_inter.followup.send(
                                "تعذّر الوصول للقناة المختارة. اختر قناة نصية أخرى.",
                                ephemeral=True,
                            )
                            return

                        view = build_color_picker_view(select_inter.guild.id, role_entries)
                        try:
                            await selected_channel.send(
                                "🎨 اختر لون اسمك بالضغط على الدائرة المناسبة:",
                                view=view,
                            )
                        except discord.Forbidden:
                            await select_inter.followup.send(
                                "لا أملك صلاحية الإرسال في القناة المختارة.",
                                ephemeral=True,
                            )
                            return
                        except Exception as e:
                            await select_inter.followup.send(
                                f"فشل نشر لوحة الألوان: {e}",
                                ephemeral=True,
                            )
                            return

                        await select_inter.followup.send(
                            f"تم نشر لوحة اختيار الألوان في {selected_channel.mention}",
                            ephemeral=True,
                        )

                class ColorChannelSelectView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=300)
                        self.add_item(
                            ColorChannelSelect(
                                placeholder="اختر القناة لنشر لوحة الألوان",
                                channel_types=[discord.ChannelType.text],
                                min_values=1,
                                max_values=1,
                            )
                        )

                await inter.response.send_message(
                    "اختر القناة التي تريد نشر لوحة الألوان فيها:",
                    view=ColorChannelSelectView(),
                    ephemeral=True,
                )

            @discord.ui.button(label="إضافة لون | Add Color", style=discord.ButtonStyle.secondary)
            async def add_color(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild:
                    await inter.response.send_message("Server only.", ephemeral=True)
                    return

                if not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
                    await inter.response.send_message("Admins only. | للمشرفين فقط.", ephemeral=True)
                    return

                class AddColorModal(discord.ui.Modal, title="إضافة لون جديد"):
                    def __init__(self):
                        super().__init__(timeout=300)
                        self.label_input = discord.ui.TextInput(
                            label="اسم اللون",
                            placeholder="مثال: سماوي",
                            max_length=30,
                        )
                        self.hex_input = discord.ui.TextInput(
                            label="HEX اللون",
                            placeholder="#00BFFF",
                            max_length=7,
                        )
                        self.emoji_input = discord.ui.TextInput(
                            label="إيموجي (اختياري)",
                            placeholder="🔹",
                            required=False,
                            max_length=2,
                        )
                        self.add_item(self.label_input)
                        self.add_item(self.hex_input)
                        self.add_item(self.emoji_input)

                    async def on_submit(self, modal_inter: discord.Interaction) -> None:
                        label = self.label_input.value.strip()
                        hex_raw = self.hex_input.value.strip().lstrip("#").upper()
                        emoji = (self.emoji_input.value or "").strip() or "⚪"

                        if not re.fullmatch(r"[0-9A-F]{6}", hex_raw):
                            await modal_inter.response.send_message(
                                "صيغة HEX غير صحيحة. مثال صحيح: #00BFFF",
                                ephemeral=True,
                            )
                            return

                        await modal_inter.response.defer(ephemeral=True, thinking=True)

                        conn2 = get_conn()
                        try:
                            existing = conn2.execute(
                                "SELECT role_id FROM color_roles WHERE guild_id = ? AND color_hex = ?",
                                (modal_inter.guild.id, hex_raw),
                            ).fetchone()
                            if existing and modal_inter.guild.get_role(existing["role_id"]):
                                await modal_inter.followup.send(
                                    "هذا اللون موجود مسبقًا.",
                                    ephemeral=True,
                                )
                                return

                            role = await modal_inter.guild.create_role(
                                name=f"Color | {label}",
                                colour=discord.Colour(int(hex_raw, 16)),
                                mentionable=False,
                                reason="Add custom color role",
                            )

                            bot_member = modal_inter.guild.me
                            if bot_member and bot_member.top_role.position > 1:
                                try:
                                    await role.edit(position=bot_member.top_role.position - 1)
                                except Exception:
                                    pass

                            conn2.execute(
                                """
                                INSERT INTO color_roles (guild_id, role_id, color_hex, label, emoji)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (modal_inter.guild.id, role.id, hex_raw, label, emoji),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()

                        await modal_inter.followup.send(
                            f"تمت إضافة اللون {emoji} {label} بنجاح. أعد نشر لوحة الألوان من زر اختر لونك.",
                            ephemeral=True,
                        )

                await inter.response.send_modal(AddColorModal())

        await interaction.response.send_message("Server Settings:", view=SettingsView(interaction.user.id, interaction.guild.id), ephemeral=True)

    @discord.ui.button(label="Help | مساعدة", style=discord.ButtonStyle.secondary)
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(t(interaction.user.id, "help"), ephemeral=True)

    @discord.ui.button(label="Update Bot | تحديث البوت", style=discord.ButtonStyle.primary)
    async def update_bot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        if not has_guild_admin_access(interaction.guild.id, interaction.user.id, interaction.guild.owner_id):
            await interaction.response.send_message(
                "Admins only. | للمشرفين فقط.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        bot_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=bot_dir,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout.decode().strip() or stderr.decode().strip())[:800]

        if proc.returncode != 0:
            await interaction.followup.send(
                f"Update failed:\n```\n{output}\n```",
                ephemeral=True,
            )
            return

        if "Already up to date" in output:
            await interaction.followup.send(
                f"Already up to date. No restart needed.\n```\n{output}\n```",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Update applied:\n```\n{output}\n```\nRestarting...",
            ephemeral=True,
        )

        async def _restart() -> None:
            await asyncio.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.create_task(_restart())


class OwnerAddAdminModal(discord.ui.Modal, title="Owner: Add Server Admin"):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_input = discord.ui.TextInput(
            label="User ID",
            placeholder="123456789012345678",
            max_length=25,
        )
        self.add_item(self.user_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.user_input.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message("User ID غير صحيح.", ephemeral=True)
            return

        user_id = int(raw)
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO admins (guild_id, user_id) VALUES (?, ?)",
                (self.guild_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()

        await interaction.response.send_message(
            f"تمت إضافة المستخدم <@{user_id}> كأدمن للسيرفر.",
            ephemeral=True,
        )


class OwnerServerSettingsView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_bot_owner(interaction.user.id):
            await interaction.response.send_message("هذه اللوحة لمالك البوت فقط.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Register Current Server | تسجيل السيرفر الحالي", style=discord.ButtonStyle.success)
    async def register_current(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        try:
            synced_count = register_current_server(interaction.guild, interaction.user.id)
        except Exception as e:
            await interaction.response.send_message(f"فشل تسجيل السيرفر: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            (
                "تم تسجيل السيرفر الحالي بنجاح.\n"
                f"Server: **{interaction.guild.name}** (`{interaction.guild.id}`)\n"
                f"Owner ID: `{interaction.guild.owner_id}`\n"
                f"Synced Text Channels: **{synced_count}**"
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Add Admin | إضافة أدمن", style=discord.ButtonStyle.primary)
    async def add_admin(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(OwnerAddAdminModal(self.guild_id))

    @discord.ui.button(label="List Registered Servers | عرض السيرفرات", style=discord.ButtonStyle.secondary)
    async def list_servers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        conn = get_conn()
        try:
            rows = conn.execute(
                """
                SELECT guild_id, guild_name, guild_owner_id, registered_by, registered_at, last_channel_sync_at
                FROM registered_servers
                ORDER BY registered_at DESC
                """
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message("لا توجد سيرفرات مسجلة حالياً.", ephemeral=True)
            return

        lines = ["السيرفرات المسجلة:"]
        for row in rows[:20]:
            in_bot = "داخل البوت" if bot.get_guild(row["guild_id"]) else "غير متاح حالياً"
            lines.append(
                (
                    f"- {row['guild_name']} (`{row['guild_id']}`) | Owner: `{row['guild_owner_id']}` | "
                    f"By: `{row['registered_by']}` | Sync: {row['last_channel_sync_at'] or '-'} | {in_bot}"
                )
            )
        if len(rows) > 20:
            lines.append(f"... +{len(rows) - 20} more")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Show Current Channels | عرض قنوات السيرفر", style=discord.ButtonStyle.secondary)
    async def show_channels(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        rows = get_registered_server_channels(interaction.guild.id, only_active=False)
        if not rows:
            await interaction.response.send_message(
                "لا توجد قنوات محفوظة لهذا السيرفر. سجل السيرفر أولاً ثم أعد المزامنة.",
                ephemeral=True,
            )
            return

        lines = [f"قنوات السيرفر `{interaction.guild.id}`:"]
        for row in rows[:30]:
            status = "active" if int(row["is_active"]) == 1 else "inactive"
            lines.append(f"- #{row['channel_name']} (`{row['channel_id']}`) | {status}")
        if len(rows) > 30:
            lines.append(f"... +{len(rows) - 30} more")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Sync Current Channels | تحديث قنوات السيرفر", style=discord.ButtonStyle.primary)
    async def sync_current(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        if not is_server_registered(interaction.guild.id):
            await interaction.response.send_message(
                "السيرفر غير مسجل. استخدم زر تسجيل السيرفر الحالي أولاً.",
                ephemeral=True,
            )
            return

        try:
            synced = sync_registered_server_channels(interaction.guild)
        except Exception as e:
            await interaction.response.send_message(f"فشلت مزامنة القنوات: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"تم تحديث قنوات السيرفر بنجاح. عدد القنوات النصية المتزامنة: {synced}",
            ephemeral=True,
        )

    @discord.ui.button(label="Sync All Registered | تحديث كل السيرفرات", style=discord.ButtonStyle.danger)
    async def sync_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        conn = get_conn()
        try:
            rows = conn.execute("SELECT guild_id FROM registered_servers ORDER BY guild_id ASC").fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.followup.send("لا توجد سيرفرات مسجلة للمزامنة.", ephemeral=True)
            return

        synced_servers = 0
        missing_servers = 0
        synced_channels_total = 0
        for row in rows:
            guild = bot.get_guild(int(row["guild_id"]))
            if guild is None:
                missing_servers += 1
                continue
            try:
                synced_channels_total += sync_registered_server_channels(guild)
                synced_servers += 1
            except Exception:
                continue

        await interaction.followup.send(
            (
                "انتهت مزامنة السيرفرات المسجلة.\n"
                f"Servers synced: **{synced_servers}**\n"
                f"Missing/Unavailable: **{missing_servers}**\n"
                f"Total channels synced: **{synced_channels_total}**"
            ),
            ephemeral=True,
        )


class MainPanelView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="إنشاء تذكير | Create", style=discord.ButtonStyle.success)
    async def create_reminder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CreateEventModal())

    @discord.ui.button(label="عرض تذكيراتي | List", style=discord.ButtonStyle.secondary)
    async def list_reminders(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE creator_id = ? AND guild_id = ? ORDER BY time ASC",
                (interaction.user.id, interaction.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message("لا توجد تذكيرات.", ephemeral=True)
            return

        lines = ["تذكيراتك القادمة:"]
        for row in rows[:20]:
            days_str = format_days_summary(str(row["days"] or ""))
            channel_str = f"<#{row['channel_id']}>" if row["channel_id"] else "Default"
            lines.append(
                f"• ID {row['id']} | {row['title']} | {row['time']} | -{row['remind_before_minutes']}m | {days_str} | {channel_str}"
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="تعديل تذكير | Edit", style=discord.ButtonStyle.primary)
    async def edit_reminder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, title, time, days, remind_before_minutes, message, image_url, channel_id FROM events WHERE creator_id = ? AND guild_id = ? ORDER BY time ASC",
                (interaction.user.id, interaction.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message("لا توجد تذكيرات.", ephemeral=True)
            return

        class EventActionView(discord.ui.View):
            def __init__(self, owner_id: int, event_row: sqlite3.Row, event_rows):
                super().__init__(timeout=300)
                self.owner_id = owner_id
                self.event_row = event_row
                self.event_rows = event_rows

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != self.owner_id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="📝 تعديل الرسالة | Message", style=discord.ButtonStyle.success)
            async def edit_message_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(
                    EditMessageModal(self.event_row["id"], self.event_row.get("message"))
                )

            @discord.ui.button(label="رفع صورة | Image", style=discord.ButtonStyle.secondary)
            async def upload_image_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_message(
                    "أرسل الصورة كمرفق في نفس القناة خلال 60 ثانية.",
                    ephemeral=True,
                )

                if not inter.channel:
                    await inter.followup.send("لا يمكن رفع صورة هنا.", ephemeral=True)
                    return

                def check(msg: discord.Message) -> bool:
                    return (
                        msg.author.id == self.owner_id
                        and msg.channel.id == inter.channel.id
                        and len(msg.attachments) > 0
                    )

                try:
                    msg = await bot.wait_for("message", timeout=60, check=check)
                except asyncio.TimeoutError:
                    await inter.followup.send("انتهى الوقت.", ephemeral=True)
                    return

                attachment = msg.attachments[0]
                if not is_image_attachment(attachment):
                    await inter.followup.send("ليست صورة.", ephemeral=True)
                    return

                class ConfirmImageView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=60)

                    @discord.ui.button(label="✅ تأكيد | Confirm", style=discord.ButtonStyle.success)
                    async def confirm_image(self, conf_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        if conf_inter.user.id != self.owner_id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET image_url = ? WHERE id = ? AND creator_id = ?",
                                (attachment.url, self.event_row["id"], self.owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await conf_inter.response.edit_message(content="✅ تم حفظ الصورة.", view=None)

                    @discord.ui.button(label="❌ إلغاء | Cancel", style=discord.ButtonStyle.danger)
                    async def cancel_image(self, conf_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        if conf_inter.user.id != self.owner_id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        await conf_inter.response.edit_message(content="تم إلغاء الرفع.", view=None)

                await inter.followup.send(
                    f"📸 تم اختيار: {attachment.filename}",
                    view=ConfirmImageView(),
                    ephemeral=True,
                )

            @discord.ui.button(label="تعديل القناة | Channel", style=discord.ButtonStyle.primary)
            async def edit_channel_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild:
                    await inter.response.send_message("Server only.", ephemeral=True)
                    return

                text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)

                class EventChannelSelect(discord.ui.Select):
                    def __init__(self):
                        options = [
                            discord.SelectOption(label="القناة الافتراضية | Default", value="default")
                        ]
                        options.extend(
                            discord.SelectOption(label=f"#{ch.name}"[:100], value=str(ch.id))
                            for ch in text_channels[:24]
                        )
                        super().__init__(
                            placeholder="اختر القناة",
                            options=options,
                            min_values=1,
                            max_values=1,
                        )

                    async def callback(self, sel_inter: discord.Interaction) -> None:
                        if sel_inter.user.id != self.owner_id:
                            await sel_inter.response.send_message("Not for you.", ephemeral=True)
                            return

                        selected = self.values[0]
                        new_channel_id = None if selected == "default" else int(selected)

                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET channel_id = ? WHERE id = ? AND creator_id = ?",
                                (new_channel_id, self.event_row["id"], self.owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()

                        channel_label = f"<#{new_channel_id}>" if new_channel_id else "Default"
                        await sel_inter.response.edit_message(
                            content=f"✅ تم التحديث.\nChannel: {channel_label}",
                            view=None,
                        )

                class EventChannelSelectView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=300)
                        self.add_item(EventChannelSelect())

                await inter.response.edit_message(
                    content="اختر القناة:",
                    view=EventChannelSelectView(),
                )

            @discord.ui.button(label="حذف | Delete", style=discord.ButtonStyle.danger)
            async def delete_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                conn2 = get_conn()
                try:
                    conn2.execute(
                        "DELETE FROM events WHERE id = ? AND creator_id = ?",
                        (self.event_row["id"], self.owner_id),
                    )
                    conn2.commit()
                finally:
                    conn2.close()

                await inter.response.edit_message(
                    content="✅ تم الحذف.",
                    view=None,
                )

            @discord.ui.button(label="رجوع | Back", style=discord.ButtonStyle.secondary)
            async def back_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.edit_message(
                    content="اختر التذكير:",
                    view=EventPickerView(self.owner_id, self.event_rows),
                )

        class EventPickerView(discord.ui.View):
            def __init__(self, owner_id: int, event_rows):
                super().__init__(timeout=300)
                self.owner_id = owner_id

                for row in event_rows[:25]:
                    label = f"{row['title'][:35]} | {row['time']}"
                    btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

                    async def on_pick(pick_inter: discord.Interaction, selected=row) -> None:
                        if pick_inter.user.id != self.owner_id:
                            await pick_inter.response.send_message("Not for you.", ephemeral=True)
                            return

                        days_str = format_days_summary(selected["days"])
                        channel_str = f"<#{selected['channel_id']}>" if selected["channel_id"] else "Default"
                        summary = (
                            f"ID {selected['id']}\n"
                            f"{selected['title']}\n"
                            f"⏰ {selected['time']} | -{selected['remind_before_minutes']}m\n"
                            f"📅 {days_str}\n"
                            f"💬 {channel_str}"
                        )
                        await pick_inter.response.edit_message(
                            content=summary,
                            view=EventActionView(self.owner_id, selected, event_rows),
                        )

                    btn.callback = on_pick
                    self.add_item(btn)

        await interaction.response.send_message(
            "اختر التذكير:",
            view=EventPickerView(interaction.user.id, rows),
            ephemeral=True,
        )

    @discord.ui.button(label="حذف تذكير | Delete", style=discord.ButtonStyle.danger)
    async def delete_reminder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, title FROM events WHERE creator_id = ? AND guild_id = ?",
                (interaction.user.id, interaction.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message("لا توجد تذكيرات.", ephemeral=True)
            return

        class DeleteView(discord.ui.View):
            def __init__(self, parent_user_id: int):
                super().__init__(timeout=300)
                self.parent_user_id = parent_user_id

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != self.parent_user_id:
                    await inter.response.send_message("Not for you.", ephemeral=True)
                    return False
                return True

            def _build_buttons(self, rows_data) -> None:
                for row in rows_data[:25]:
                    def make_delete(event_id: int):
                        async def delete_callback(inter: discord.Interaction) -> None:
                            conn2 = get_conn()
                            try:
                                conn2.execute(
                                    "DELETE FROM events WHERE id = ? AND creator_id = ?",
                                    (event_id, self.parent_user_id),
                                )
                                conn2.commit()
                            finally:
                                conn2.close()
                            await inter.response.send_message("✅ تم الحذف.", ephemeral=True)
                        return delete_callback

                    btn = discord.ui.Button(
                        label=f"🗑 {row['title'][:22]}",
                        style=discord.ButtonStyle.danger,
                    )
                    btn.callback = make_delete(row["id"])
                    self.add_item(btn)

        dv = DeleteView(interaction.user.id)
        dv._build_buttons(rows)

        await interaction.response.send_message(
            "اختر التذكير للحذف:",
            view=dv,
            ephemeral=True,
        )

    @discord.ui.button(label="إعدادات ServerServer | Config", style=discord.ButtonStyle.secondary)
    async def server_config(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT notification_channel_id FROM server_settings WHERE guild_id = ?",
                (interaction.guild.id,),
            ).fetchone()
        finally:
            conn.close()

        if not row or not row["notification_channel_id"]:
            await interaction.response.send_message(
                "السيرفر غير مسجل. استخدم `/setup`.",
                ephemeral=True,
            )
            return

        channel_id = row["notification_channel_id"]

        class ServerConfigView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)

            @discord.ui.button(label="عرض الإعدادات | View", style=discord.ButtonStyle.secondary)
            async def view_config(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_message(
                    f"⚙️ إعدادات السيرفر:\n"
                    f"معرّف السيرفر: `{interaction.guild.id}`\n"
                    f"قناة التذكيرات: <#{channel_id}>",
                    ephemeral=True,
                )

        await interaction.response.send_message(
            "إعدادات السيرفر:",
            view=ServerConfigView(),
            ephemeral=True,
        )


class PanelHomeView(discord.ui.View):
    """الشاشة الرئيسية للوحة التحكم"""
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("ليس لديك صلاحية.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📋 التذكيرات | Reminders", style=discord.ButtonStyle.primary)
    async def reminders_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "التذكيرات:",
            view=MainPanelView(owner_id=self.owner_id),
            ephemeral=True,
        )

    @discord.ui.button(label="⚙️ الإعدادات | Settings", style=discord.ButtonStyle.secondary)
    async def settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        class SettingsPanelView(discord.ui.View):
            def __init__(self, owner_id: int, guild_id: int):
                super().__init__(timeout=600)
                self.owner_id = owner_id
                self.guild_id = guild_id

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != self.owner_id:
                    await inter.response.send_message("ليس لديك صلاحية.", ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="👥 المشرفين | Admins", style=discord.ButtonStyle.primary)
            async def admins_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                conn = get_conn()
                try:
                    admins = conn.execute(
                        "SELECT admin_user_id FROM admins WHERE guild_id = ?",
                        (self.guild_id,),
                    ).fetchall()
                finally:
                    conn.close()

                if not admins:
                    await inter.response.send_message(
                        "لا توجد مشرفين مضافين.",
                        ephemeral=True,
                    )
                    return

                admin_list = "\n".join(f"• <@{a['admin_user_id']}>" for a in admins[:20])
                class ManageAdminsView(discord.ui.View):
                    def __init__(self, owner_id: int, guild_id: int):
                        super().__init__(timeout=300)
                        self.owner_id = owner_id
                        self.guild_id = guild_id

                    @discord.ui.button(label="➕ إضافة | Add", style=discord.ButtonStyle.success)
                    async def add_admin(self, add_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        class PickUserModal(discord.ui.Modal, title="أدخل معرف المشرف"):
                            admin_id_input = discord.ui.TextInput(label="معرف المشرف (User ID)", max_length=20)

                            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                                try:
                                    admin_id = int(self.admin_id_input.value)
                                    conn = get_conn()
                                    try:
                                        conn.execute(
                                            "INSERT OR IGNORE INTO admins (guild_id, admin_user_id) VALUES (?, ?)",
                                            (self.guild_id, admin_id),
                                        )
                                        conn.commit()
                                    finally:
                                        conn.close()
                                    await modal_inter.response.send_message(
                                        f"✅ تم إضافة <@{admin_id}> كمشرف.",
                                        ephemeral=True,
                                    )
                                except ValueError:
                                    await modal_inter.response.send_message(
                                        "معرف غير صحيح.",
                                        ephemeral=True,
                                    )

                        await add_inter.response.send_modal(PickUserModal())

                    @discord.ui.button(label="❌ حذف | Remove", style=discord.ButtonStyle.danger)
                    async def remove_admin(self, remove_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        class PickUserModal(discord.ui.Modal, title="أدخل معرف المشرف للحذف"):
                            admin_id_input = discord.ui.TextInput(label="معرف المشرف (User ID)", max_length=20)

                            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                                try:
                                    admin_id = int(self.admin_id_input.value)
                                    conn = get_conn()
                                    try:
                                        conn.execute(
                                            "DELETE FROM admins WHERE guild_id = ? AND admin_user_id = ?",
                                            (self.guild_id, admin_id),
                                        )
                                        conn.commit()
                                    finally:
                                        conn.close()
                                    await modal_inter.response.send_message(
                                        f"✅ تم حذف المشرف.",
                                        ephemeral=True,
                                    )
                                except ValueError:
                                    await modal_inter.response.send_message(
                                        "معرف غير صحيح.",
                                        ephemeral=True,
                                    )

                        await remove_inter.response.send_modal(PickUserModal())

                await inter.response.send_message(
                    f"المشرفين ({len(admins)}):\n{admin_list}",
                    view=ManageAdminsView(self.owner_id, self.guild_id),
                    ephemeral=True,
                )

            @discord.ui.button(label="📢 قناة التذكيرات | Channel", style=discord.ButtonStyle.secondary)
            async def channel_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                conn = get_conn()
                try:
                    settings = conn.execute(
                        "SELECT notification_channel_id FROM server_settings WHERE guild_id = ?",
                        (self.guild_id,),
                    ).fetchone()
                finally:
                    conn.close()

                channel_id = settings["notification_channel_id"] if settings else None
                channel_text = f"<#{channel_id}>" if channel_id else "لم تُحدد"

                await inter.response.send_message(
                    f"📢 قناة التذكيرات: {channel_text}",
                    ephemeral=True,
                )

            @discord.ui.button(label="🔄 رجوع | Back", style=discord.ButtonStyle.secondary)
            async def back_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.edit_message(
                    content="اختر:",
                    view=PanelHomeView(self.owner_id, self.guild_id),
                )

        await interaction.response.send_message(
            "الإعدادات:",
            view=SettingsPanelView(self.owner_id, self.guild_id),
            ephemeral=True,
        )


@bot.tree.command(name="panel", description="Open control panel | فتح لوحة التحكم")
async def panel(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    await interaction.response.send_message(
        "🎮 لوحة التحكم",
        view=PanelHomeView(owner_id=interaction.user.id, guild_id=interaction.guild.id),
        ephemeral=True,
    )


@bot.tree.command(name="owner_settings", description="Bot owner settings | إعدادات البوت للمالك")
async def owner_settings(interaction: discord.Interaction) -> None:
    """إعدادات خاصة بمالك البوت فقط"""
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "هذا الأمر خاص بمالك البوت فقط.",
            ephemeral=True,
        )
        return

    class OwnerSettingsView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=600)

        @discord.ui.button(label="ℹ️ عن البوت | About", style=discord.ButtonStyle.primary)
        async def about_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            embed = discord.Embed(
                title="🤖 عن البوت",
                description="بوت التذكيرات المتقدم",
                color=discord.Color.gold(),
            )
            embed.add_field(name="👤 صانع البوت", value="DANGER TNT", inline=False)
            embed.add_field(name="🆔 معرّف الديسكورد", value="DANGER_600", inline=False)
            embed.add_field(name="🌐 الريبو", value="[BOT1](https://github.com/mansour305x/BOT1)", inline=False)
            embed.add_field(name="✨ الميزات", value="تذكيرات متقدمة، دعم عربي، إعدادات مرنة", inline=False)
            
            await inter.response.send_message(embed=embed, ephemeral=True)

        @discord.ui.button(label="📞 التواصل | Contact", style=discord.ButtonStyle.secondary)
        async def contact_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            await inter.response.send_message(
                "📞 **للتواصل مع الدعم:**\n"
                "🔗 Mention: <@DANGER_600>\n"
                "💬 Discord: DANGER_600\n\n"
                "سيتم الرد عليك قريباً!",
                ephemeral=True,
            )

    await interaction.response.send_message(
        "⚙️ إعدادات البوت",
        view=OwnerSettingsView(),
        ephemeral=True,
    )


@bot.tree.command(name="setup", description="Configure bot for this server | إعداد البوت للسيرفر")
async def setup(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    if not has_guild_admin_access(interaction.guild.id, interaction.user.id, interaction.guild.owner_id):
        await interaction.response.send_message(
            "Admins only. | للمشرفين فقط.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id

    # Channel-only setup (no role selection).
    async def on_channel_selected(inter: discord.Interaction, channel_id: int) -> None:
        conn2 = get_conn()
        try:
            conn2.execute(
                """
                INSERT INTO server_settings (guild_id, notification_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    notification_channel_id = excluded.notification_channel_id
                """,
                (guild_id, channel_id),
            )
            conn2.commit()
        finally:
            conn2.close()

        ch_mention = f"<#{channel_id}>"
        await inter.response.edit_message(
            content=(
                f"✅ Setup complete! | تم الإعداد!\n"
                f"📢 Notification channel | قناة التنبيهات: {ch_mention}"
            ),
            view=None,
        )

    # Step 1: show channel selector
    class ChannelSelectSetup(discord.ui.ChannelSelect):
        async def callback(self, ch_inter: discord.Interaction) -> None:
            selected_channel = self.values[0] if self.values else None
            if not selected_channel:
                await ch_inter.response.send_message("No channel selected.", ephemeral=True)
                return
            await on_channel_selected(ch_inter, selected_channel.id)

    class ChannelSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.add_item(ChannelSelectSetup(
                placeholder="اختر القناة | Select channel",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
            ))

    await interaction.response.send_message(
        "**Server Setup | إعداد السيرفر**\n\nاختر القناة التي ستُرسَل فيها التذكيرات | Select the channel for reminders:",
        view=ChannelSelectView(),
        ephemeral=True,
    )


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN in environment.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    bot.run(token)


if __name__ == "__main__":
    main()
