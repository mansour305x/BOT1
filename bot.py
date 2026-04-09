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
    if "is_paused" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN is_paused INTEGER NOT NULL DEFAULT 0")
    if "embed_color" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN embed_color TEXT")
    if "ping_type" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN ping_type TEXT NOT NULL DEFAULT 'everyone'")
    
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
    if "notification_role_id" not in ss_columns:
        conn.execute("ALTER TABLE server_settings ADD COLUMN notification_role_id INTEGER")
    if "default_embed_color" not in ss_columns:
        conn.execute("ALTER TABLE server_settings ADD COLUMN default_embed_color TEXT")
    if "default_ping_type" not in ss_columns:
        conn.execute("ALTER TABLE server_settings ADD COLUMN default_ping_type TEXT NOT NULL DEFAULT 'everyone'")
    
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_defaults (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            default_time TEXT,
            default_days TEXT,
            default_channel_id INTEGER,
            default_remind_before INTEGER NOT NULL DEFAULT 10,
            PRIMARY KEY (user_id, guild_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_global_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
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


def bi_text(ar: str, en: str) -> str:
    """Return a bilingual text in Arabic and English."""
    return f"{ar} | {en}"


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


async def request_image_attachment(
    interaction: discord.Interaction,
    owner_id: int,
    *,
    timeout: int = 60,
) -> Optional[discord.Attachment]:
    """Ask user to upload an image attachment and return it if valid."""
    await interaction.response.send_message(
        "ارسل الصورة كمرفق في نفس القناة خلال 60 ثانية.",
        ephemeral=True,
    )

    if not interaction.channel:
        await interaction.followup.send("لا يمكن رفع صورة هنا.", ephemeral=True)
        return None

    def check(msg: discord.Message) -> bool:
        return (
            msg.author.id == owner_id
            and msg.channel.id == interaction.channel.id
            and len(msg.attachments) > 0
        )

    try:
        msg = await bot.wait_for("message", timeout=timeout, check=check)
    except asyncio.TimeoutError:
        await interaction.followup.send("انتهى الوقت، حاول مرة اخرى.", ephemeral=True)
        return None

    attachment = msg.attachments[0]
    if not is_image_attachment(attachment):
        await interaction.followup.send("المرفق ليس صورة صالحة.", ephemeral=True)
        return None

    return attachment


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

                try:
                    if int(row["is_paused"] or 0):
                        continue
                except Exception:
                    pass

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
        try:
            if int(row["is_paused"] or 0):
                return
        except Exception:
            pass

        guild = self.get_guild(row["guild_id"])
        if not guild:
            logging.warning(f"Guild {row['guild_id']} not found")
            return

        lang = get_user_lang(row["creator_id"])
        msg_dict = MESSAGES.get(lang, MESSAGES["en"])

        embed_color = discord.Color.blurple()
        try:
            if row["embed_color"]:
                embed_color = discord.Color(int(str(row["embed_color"]), 16))
        except Exception:
            pass

        embed = discord.Embed(
            title=msg_dict["event_reminder_title"],
            color=embed_color,
        )
        embed.add_field(name=msg_dict["event_field_name"], value=row["title"], inline=False)
        if row["message"]:
            embed.add_field(name="Message", value=row["message"], inline=False)
        if row["image_url"]:
            embed.set_image(url=row["image_url"])

        conn = get_conn()
        try:
            settings = conn.execute(
                "SELECT notification_channel_id, notification_role_id FROM server_settings WHERE guild_id = ?",
                (row["guild_id"],),
            ).fetchone()
        finally:
            conn.close()

        try:
            ping_type = str(row["ping_type"] or "everyone")
        except Exception:
            ping_type = "everyone"

        if ping_type == "everyone":
            mention = "@everyone"
        elif ping_type == "here":
            mention = "@here"
        elif ping_type == "role":
            role_id = settings["notification_role_id"] if settings and settings["notification_role_id"] else None
            mention = f"<@&{role_id}>" if role_id else "@everyone"
        else:
            mention = ""

        channel = None
        if row["channel_id"]:
            channel = guild.get_channel(int(row["channel_id"]))
        if channel is None and settings and settings["notification_channel_id"]:
            channel = guild.get_channel(settings["notification_channel_id"])
        if channel is None:
            channel = guild.text_channels[0] if guild.text_channels else None
        if channel:
            try:
                await channel.send(
                    content=mention,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(everyone=True, roles=True),
                )
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

        # Load user defaults
        _conn = get_conn()
        try:
            _defaults = _conn.execute(
                "SELECT * FROM user_defaults WHERE user_id = ? AND guild_id = ?",
                (interaction.user.id, interaction.guild.id),
            ).fetchone()
        finally:
            _conn.close()
        defaults = dict(_defaults) if _defaults else {}

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

                attachment = await request_image_attachment(inter, interaction.user.id)
                if not attachment:
                    return

                class ConfirmImageView(discord.ui.View):
                    def __init__(self) -> None:
                        super().__init__(timeout=60)

                    @discord.ui.button(label="✅ تأكيد", style=discord.ButtonStyle.success)
                    async def confirm_image(self, conf_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        if conf_inter.user.id != interaction.user.id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        modal_self.selected_image_url = attachment.url
                        await conf_inter.response.edit_message(content="✅ تم حفظ الصورة.", view=None)

                    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
                    async def cancel_image(self, conf_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        if conf_inter.user.id != interaction.user.id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        await conf_inter.response.edit_message(content="تم إلغاء رفع الصورة.", view=None)

                await inter.followup.send(
                    f"📸 تم اختيار: {attachment.filename}",
                    view=ConfirmImageView(),
                    ephemeral=True,
                )

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

            class PostCreateActionsView(discord.ui.View):
                def __init__(self, owner_id: int, created_event_id: int):
                    super().__init__(timeout=300)
                    self.owner_id = owner_id
                    self.created_event_id = created_event_id

                async def interaction_check(self, action_inter: discord.Interaction) -> bool:
                    if action_inter.user.id != self.owner_id:
                        await action_inter.response.send_message("Not for you.", ephemeral=True)
                        return False
                    return True

                @discord.ui.button(label="🧪 اختبار التذكير الآن", style=discord.ButtonStyle.primary)
                async def test_now_btn(self, action_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                    conn2 = get_conn()
                    try:
                        row = conn2.execute(
                            "SELECT * FROM events WHERE id = ? AND creator_id = ?",
                            (self.created_event_id, self.owner_id),
                        ).fetchone()
                    finally:
                        conn2.close()

                    if not row:
                        await action_inter.response.send_message("لم يتم العثور على التذكير.", ephemeral=True)
                        return

                    await bot.send_event_reminder(row)
                    await action_inter.response.send_message(
                        "✅ تم إرسال تذكير اختباري الآن.",
                        ephemeral=True,
                    )

            await inter.response.edit_message(
                content=(
                    f"{t(interaction.user.id, 'event_created', event_id=event_id)}\n"
                    "يمكنك الآن اختبار التذكير فوراً من الزر أدناه."
                ),
                view=PostCreateActionsView(interaction.user.id, event_id),
            )

        # إذا وجدت إعدادات افتراضية مكتملة، اعرض خيار استخدامها
        has_full_defaults = bool(
            defaults.get("default_days") and defaults.get("default_remind_before")
        )

        def _apply_defaults() -> None:
            modal_self.selected_days = defaults["default_days"]
            modal_self.selected_remind_before = defaults["default_remind_before"]
            if defaults.get("default_channel_id"):
                modal_self.selected_channel_id = defaults["default_channel_id"]

        if has_full_defaults:
            days_label = format_days_summary(str(defaults["default_days"]))
            ch_label = f"<#{defaults['default_channel_id']}>" if defaults.get("default_channel_id") else "الافتراضية"

            class UseDefaultsView(discord.ui.View):
                def __init__(self) -> None:
                    super().__init__(timeout=300)

                async def interaction_check(self, inter: discord.Interaction) -> bool:
                    if inter.user.id != interaction.user.id:
                        await inter.response.send_message("Not for you.", ephemeral=True)
                        return False
                    return True

                @discord.ui.button(label="⚡ استخدم الإعدادات الافتراضية", style=discord.ButtonStyle.success)
                async def use_defaults_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                    _apply_defaults()
                    await show_summary(inter)

                @discord.ui.button(label="🔧 تخصيص", style=discord.ButtonStyle.secondary)
                async def customize_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                    await inter.response.edit_message(
                        content="اختر الأيام:",
                        view=DaysSelectView(on_days_selected, interaction.user.id, include_alt_start=True),
                    )

            await interaction.response.send_message(
                content=(
                    f"**إعداداتك الافتراضية:**\n"
                    f"📅 الأيام: `{days_label}`\n"
                    f"🔔 التنبيه: `{defaults['default_remind_before']} دقيقة قبل`\n"
                    f"📢 القناة: {ch_label}\n\n"
                    f"هل تريد استخدامها أم تخصيص الإعدادات؟"
                ),
                view=UseDefaultsView(),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content="اختر الأيام:",
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
                captured_event_id = self.event_row["id"]
                captured_owner_id = self.owner_id
                attachment = await request_image_attachment(inter, captured_owner_id)
                if not attachment:
                    return

                class ConfirmImageView(discord.ui.View):
                    def __init__(self) -> None:
                        super().__init__(timeout=60)

                    @discord.ui.button(label="✅ تأكيد الحفظ", style=discord.ButtonStyle.success)
                    async def confirm_image(self, conf_inter: discord.Interaction, button2: discord.ui.Button) -> None:
                        if conf_inter.user.id != captured_owner_id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET image_url = ? WHERE id = ? AND creator_id = ?",
                                (attachment.url, captured_event_id, captured_owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await conf_inter.response.edit_message(content="✅ تم تحديث صورة التذكير.", view=None)

                    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
                    async def cancel_image(self, conf_inter: discord.Interaction, button2: discord.ui.Button) -> None:
                        if conf_inter.user.id != captured_owner_id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        await conf_inter.response.edit_message(content="تم إلغاء التحديث.", view=None)

                await inter.followup.send(
                    f"📸 تم اختيار: {attachment.filename}\nهل تريد حفظها لهذا التذكير؟",
                    view=ConfirmImageView(),
                    ephemeral=True,
                )

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

    @discord.ui.button(label="🚀 أدوات متقدمة | Advanced Tools", style=discord.ButtonStyle.danger)
    async def advanced_tools_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            bi_text("🚀 الأدوات المتقدمة لمالك البوت:", "🚀 Owner advanced tools:"),
            view=OwnerAdvancedView(owner_id=interaction.user.id, guild_id=self.guild_id),
            ephemeral=True,
        )


class OwnerAdvancedView(discord.ui.View):
    """Advanced tools for bot owner."""

    def __init__(self, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not is_bot_owner(interaction.user.id):
            await interaction.response.send_message("هذه اللوحة لمالك البوت فقط.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📊 إحصائيات البوت | Bot Statistics", style=discord.ButtonStyle.primary, row=0)
    async def bot_stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        conn = get_conn()
        try:
            total_events = conn.execute("SELECT COUNT(*) as cnt FROM events").fetchone()["cnt"]
            paused_events = conn.execute("SELECT COUNT(*) as cnt FROM events WHERE is_paused = 1").fetchone()["cnt"]
            total_registered = conn.execute("SELECT COUNT(*) as cnt FROM registered_servers").fetchone()["cnt"]
            distinct_creators = conn.execute("SELECT COUNT(DISTINCT creator_id) as cnt FROM events").fetchone()["cnt"]
        finally:
            conn.close()

        total_guilds = len(bot.guilds)
        await interaction.response.send_message(
            f"**📊 {bi_text('إحصائيات البوت', 'Bot Statistics')}:**\n"
            f"🌐 {bi_text('السيرفرات الكلية', 'Total guilds')}: **{total_guilds}**\n"
            f"📝 {bi_text('السيرفرات المسجلة', 'Registered servers')}: **{total_registered}**\n"
            f"📋 {bi_text('التذكيرات الكلية', 'Total reminders')}: **{total_events}**\n"
            f"⏸️ {bi_text('التذكيرات الموقوفة', 'Paused reminders')}: **{paused_events}**\n"
            f"👥 {bi_text('مستخدمون لديهم تذكيرات', 'Users with reminders')}: **{distinct_creators}**",
            ephemeral=True,
        )

    @discord.ui.button(label="📣 رسالة جماعية | Broadcast", style=discord.ButtonStyle.primary, row=0)
    async def broadcast_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        class BroadcastModal(discord.ui.Modal, title="Broadcast Message"):
            def __init__(self) -> None:
                super().__init__(timeout=300)
                self.msg_input = discord.ui.TextInput(
                    label="Message",
                    style=discord.TextStyle.paragraph,
                    max_length=1800,
                )
                self.add_item(self.msg_input)

            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                await modal_inter.response.defer(ephemeral=True, thinking=True)
                msg_text = self.msg_input.value.strip()

                conn2 = get_conn()
                try:
                    reg_rows = conn2.execute("SELECT guild_id FROM registered_servers").fetchall()
                finally:
                    conn2.close()

                async def send_to_guild(gid: int) -> None:
                    g = bot.get_guild(gid)
                    if not g or not g.text_channels:
                        return
                    try:
                        await g.text_channels[0].send(msg_text)
                    except Exception:
                        pass

                tasks = [send_to_guild(int(r["guild_id"])) for r in reg_rows]
                await asyncio.gather(*tasks, return_exceptions=True)
                await modal_inter.followup.send(
                    f"✅ {bi_text('تم إرسال الرسالة إلى', 'Message sent to')} {len(reg_rows)} {bi_text('سيرفر', 'servers')}.",
                    ephemeral=True,
                )

        await interaction.response.send_modal(BroadcastModal())

    @discord.ui.button(label="🎭 حالة البوت | Bot Status", style=discord.ButtonStyle.secondary, row=0)
    async def bot_status_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        class StatusModal(discord.ui.Modal, title="Set Bot Status"):
            def __init__(self) -> None:
                super().__init__(timeout=300)
                self.activity_type = discord.ui.TextInput(
                    label="Activity Type (playing/watching/listening)",
                    placeholder="playing",
                    max_length=20,
                    default="playing",
                )
                self.activity_text = discord.ui.TextInput(
                    label="Activity Text",
                    placeholder="with reminders",
                    max_length=128,
                )
                self.add_item(self.activity_type)
                self.add_item(self.activity_text)

            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                atype = self.activity_type.value.strip().lower()
                atext = self.activity_text.value.strip()
                if atype == "watching":
                    activity = discord.Activity(type=discord.ActivityType.watching, name=atext)
                elif atype == "listening":
                    activity = discord.Activity(type=discord.ActivityType.listening, name=atext)
                else:
                    activity = discord.Game(name=atext)
                await bot.change_presence(activity=activity)
                await modal_inter.response.send_message(
                    f"✅ {bi_text('تم تعيين الحالة', 'Status set')}: {atype} {atext}",
                    ephemeral=True,
                )

        await interaction.response.send_modal(StatusModal())

    @discord.ui.button(label="📋 عرض كل التذكيرات | All Reminders", style=discord.ButtonStyle.secondary, row=0)
    async def all_reminders_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, guild_id, creator_id, title, time, days FROM events ORDER BY guild_id, time LIMIT 20"
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await interaction.response.send_message(bi_text("لا توجد تذكيرات.", "No reminders found."), ephemeral=True)
            return

        lines = [f"**{bi_text('التذكيرات (أول 20)', 'Reminders (first 20)')}:**"]
        for row in rows:
            lines.append(f"• #{row['id']} | GID:{row['guild_id']} | {row['title'][:30]} | {row['time']}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="🌐 رسالة افتراضية | Default Template", style=discord.ButtonStyle.secondary, row=1)
    async def global_template_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        class TemplateModal(discord.ui.Modal, title="Global Default Message Template"):
            def __init__(self) -> None:
                super().__init__(timeout=300)
                self.template_input = discord.ui.TextInput(
                    label="Template",
                    style=discord.TextStyle.paragraph,
                    required=False,
                    max_length=500,
                )
                self.add_item(self.template_input)

            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                template = self.template_input.value.strip() or ""
                conn2 = get_conn()
                try:
                    conn2.execute(
                        """
                        INSERT INTO bot_global_settings (key, value, updated_at)
                        VALUES ('default_message_template', ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (template, dt.datetime.now().isoformat()),
                    )
                    conn2.commit()
                finally:
                    conn2.close()
                await modal_inter.response.send_message(
                    f"✅ {bi_text('تم تعيين القالب الافتراضي.', 'Default template set.')}",
                    ephemeral=True,
                )

        await interaction.response.send_modal(TemplateModal())

    @discord.ui.button(label="🔄 إعادة تشغيل | Force Restart", style=discord.ButtonStyle.danger, row=1)
    async def force_restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            bi_text("⚠️ سيتم إعادة تشغيل البوت خلال ثانيتين...", "⚠️ Bot will restart in 2 seconds..."),
            ephemeral=True,
        )

        async def _restart() -> None:
            await asyncio.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        asyncio.create_task(_restart())


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

            @discord.ui.button(label="⏰ تعديل الوقت | Time", style=discord.ButtonStyle.primary)
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

            @discord.ui.button(label="📝 تعديل الرسالة | Message", style=discord.ButtonStyle.success)
            async def edit_message_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(
                    EditMessageModal(self.event_row["id"], self.event_row["message"])
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

                captured_owner_id = self.owner_id
                captured_event_id = self.event_row["id"]

                class ConfirmImageView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=60)

                    @discord.ui.button(label="✅ تأكيد | Confirm", style=discord.ButtonStyle.success)
                    async def confirm_image(self, conf_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        if conf_inter.user.id != captured_owner_id:
                            await conf_inter.response.send_message("Not for you.", ephemeral=True)
                            return
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET image_url = ? WHERE id = ? AND creator_id = ?",
                                (attachment.url, captured_event_id, captured_owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await conf_inter.response.edit_message(content="✅ تم حفظ الصورة.", view=None)

                    @discord.ui.button(label="❌ إلغاء | Cancel", style=discord.ButtonStyle.danger)
                    async def cancel_image(self, conf_inter: discord.Interaction, btn: discord.ui.Button) -> None:
                        if conf_inter.user.id != captured_owner_id:
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
                        if sel_inter.user.id != self.view.owner_id:
                            await sel_inter.response.send_message("Not for you.", ephemeral=True)
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
                        await sel_inter.response.edit_message(
                            content=f"✅ تم التحديث.\nChannel: {channel_label}",
                            view=None,
                        )

                class EventChannelSelectView(discord.ui.View):
                    def __init__(self, owner_id: int, event_id: int):
                        super().__init__(timeout=300)
                        self.owner_id = owner_id
                        self.event_id = event_id
                        self.add_item(EventChannelSelect())

                await inter.response.edit_message(
                    content="اختر القناة:",
                    view=EventChannelSelectView(self.owner_id, self.event_row["id"]),
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


class RemindersView(discord.ui.View):
    """قسم تذكيرات متقدم مع إدارة كاملة للتذكيرات"""

    def __init__(self, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(bi_text("ليس لديك صلاحية.", "You do not have permission."), ephemeral=True)
            return False
        return True

    async def _fetch_events(self, user_id: int, guild_id: int) -> list[sqlite3.Row]:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE creator_id = ? AND guild_id = ? ORDER BY time ASC, id ASC",
                (user_id, guild_id),
            ).fetchall()
            return list(rows)
        finally:
            conn.close()

    async def _show_events_manager(self, interaction: discord.Interaction, title: str = "إدارة التذكيرات") -> None:
        if not interaction.guild:
            await interaction.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
            return

        events_rows = await self._fetch_events(interaction.user.id, interaction.guild.id)
        if not events_rows:
            await interaction.response.send_message(bi_text("لا توجد تذكيرات بعد.", "No reminders yet."), ephemeral=True)
            return

        owner_id = self.owner_id
        guild_id = self.guild_id

        class EventActionsView(discord.ui.View):
            def __init__(self, selected_event: sqlite3.Row, all_rows: list[sqlite3.Row]) -> None:
                super().__init__(timeout=300)
                self.selected_event = selected_event
                self.all_rows = all_rows

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != owner_id:
                    await inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="✍️ تعديل الرسالة | Edit Message", style=discord.ButtonStyle.secondary, row=0)
            async def edit_message_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(
                    EditMessageModal(self.selected_event["id"], self.selected_event["message"])
                )

            @discord.ui.button(label="⏰ تعديل الوقت والأيام | Edit Time/Days", style=discord.ButtonStyle.primary, row=0)
            async def edit_schedule_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(
                    EditScheduleModal(
                        event_id=self.selected_event["id"],
                        current_title=self.selected_event["title"],
                        current_time=self.selected_event["time"],
                        current_days=self.selected_event["days"],
                        current_remind_before=int(self.selected_event["remind_before_minutes"]),
                    )
                )

            @discord.ui.button(label="🖼️ تعديل الصورة | Edit Image", style=discord.ButtonStyle.secondary, row=0)
            async def edit_image_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                attachment = await request_image_attachment(inter, owner_id)
                if not attachment:
                    return

                event_id = self.selected_event["id"]

                class ConfirmImageView(discord.ui.View):
                    def __init__(self) -> None:
                        super().__init__(timeout=60)

                    @discord.ui.button(label="✅ حفظ الصورة", style=discord.ButtonStyle.success)
                    async def confirm_save(self, conf_inter: discord.Interaction, b: discord.ui.Button) -> None:
                        if conf_inter.user.id != owner_id:
                            await conf_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET image_url = ? WHERE id = ? AND creator_id = ?",
                                (attachment.url, event_id, owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await conf_inter.response.edit_message(content=bi_text("✅ تم حفظ الصورة بنجاح.", "✅ Image saved successfully."), view=None)

                    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
                    async def cancel_save(self, conf_inter: discord.Interaction, b: discord.ui.Button) -> None:
                        if conf_inter.user.id != owner_id:
                            await conf_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return
                        await conf_inter.response.edit_message(content=bi_text("تم الإلغاء.", "Cancelled."), view=None)

                await inter.followup.send(
                    f"{bi_text('📸 تم اختيار الصورة', '📸 Selected image')}: {attachment.filename}\n{bi_text('هل تريد حفظها لهذا التذكير؟', 'Do you want to save it for this reminder?')}",
                    view=ConfirmImageView(),
                    ephemeral=True,
                )

            @discord.ui.button(label="📢 تعديل القناة | Channel", style=discord.ButtonStyle.primary, row=0)
            async def edit_channel_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild:
                    await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
                    return

                text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)
                ev_id = self.selected_event["id"]

                class EvChSelect(discord.ui.Select):
                    def __init__(self):
                        options = [discord.SelectOption(label=bi_text("القناة الافتراضية", "Default channel"), value="default")]
                        options.extend(
                            discord.SelectOption(label=f"#{ch.name}"[:100], value=str(ch.id))
                            for ch in text_channels[:24]
                        )
                        super().__init__(placeholder=bi_text("اختر القناة", "Select channel"), options=options, min_values=1, max_values=1)

                    async def callback(self, sel_inter: discord.Interaction) -> None:
                        if sel_inter.user.id != owner_id:
                            await sel_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return
                        selected = self.values[0]
                        new_ch_id = None if selected == "default" else int(selected)
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET channel_id = ? WHERE id = ? AND creator_id = ?",
                                (new_ch_id, ev_id, owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        ch_label = f"<#{new_ch_id}>" if new_ch_id else bi_text("الافتراضية", "Default")
                        await sel_inter.response.edit_message(
                            content=f"✅ {bi_text('تم تحديث القناة', 'Channel updated')}: {ch_label}",
                            view=None,
                        )

                class EvChSelectView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=300)
                        self.add_item(EvChSelect())

                await inter.response.edit_message(
                    content=bi_text("اختر القناة الجديدة لهذا التذكير:", "Choose new channel for this reminder:"),
                    view=EvChSelectView(),
                )

            @discord.ui.button(label="⏸️ إيقاف | Pause", style=discord.ButtonStyle.secondary, row=0)
            async def pause_resume_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                conn2 = get_conn()
                try:
                    current_row = conn2.execute(
                        "SELECT is_paused FROM events WHERE id = ? AND creator_id = ?",
                        (self.selected_event["id"], owner_id),
                    ).fetchone()
                    if not current_row:
                        await inter.response.send_message(bi_text("التذكير غير موجود.", "Reminder not found."), ephemeral=True)
                        return
                    new_paused = 0 if int(current_row["is_paused"] or 0) else 1
                    conn2.execute(
                        "UPDATE events SET is_paused = ? WHERE id = ? AND creator_id = ?",
                        (new_paused, self.selected_event["id"], owner_id),
                    )
                    conn2.commit()
                finally:
                    conn2.close()

                if new_paused:
                    msg = bi_text("⏸️ تم إيقاف التذكير مؤقتاً.", "⏸️ Reminder paused.")
                else:
                    msg = bi_text("▶️ تم تفعيل التذكير.", "▶️ Reminder resumed.")
                await inter.response.send_message(msg, ephemeral=True)

            @discord.ui.button(label="🧪 اختبار موعد التذكير | Test Reminder", style=discord.ButtonStyle.primary, row=1)
            async def test_event_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.defer(ephemeral=True, thinking=True)
                try:
                    await bot.send_event_reminder(self.selected_event)
                except Exception as exc:
                    await inter.followup.send(
                        f"{bi_text('❌ فشل إرسال التذكير التجريبي', '❌ Failed to send test reminder')}: {exc}",
                        ephemeral=True,
                    )
                    return

                await inter.followup.send(
                    bi_text("✅ تم إرسال تذكير تجريبي لهذا الموعد.", "✅ Test reminder sent for this schedule."),
                    ephemeral=True,
                )

            @discord.ui.button(label="🎨 لون التذكير | Color", style=discord.ButtonStyle.secondary, row=1)
            async def color_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                ev_id = self.selected_event["id"]

                class ColorSelectView(discord.ui.View):
                    def __init__(self) -> None:
                        super().__init__(timeout=300)

                        def make_color_cb(hex_val: str, color_label: str):
                            async def color_cb(color_inter: discord.Interaction) -> None:
                                if color_inter.user.id != owner_id:
                                    await color_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                                    return
                                conn2 = get_conn()
                                try:
                                    conn2.execute(
                                        "UPDATE events SET embed_color = ? WHERE id = ? AND creator_id = ?",
                                        (hex_val, ev_id, owner_id),
                                    )
                                    conn2.commit()
                                finally:
                                    conn2.close()
                                await color_inter.response.edit_message(
                                    content=f"✅ {bi_text('تم تعيين اللون', 'Color set')}: {color_label}",
                                    view=None,
                                )
                            return color_cb

                        for idx, (clabel, chex, cemoji) in enumerate(COLOR_PRESETS):
                            cb = discord.ui.Button(label=f"{cemoji} {clabel}", style=discord.ButtonStyle.secondary, row=idx // 5)
                            cb.callback = make_color_cb(chex, clabel)
                            self.add_item(cb)

                        async def reset_color(reset_inter: discord.Interaction) -> None:
                            if reset_inter.user.id != owner_id:
                                await reset_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                                return
                            conn2 = get_conn()
                            try:
                                conn2.execute(
                                    "UPDATE events SET embed_color = NULL WHERE id = ? AND creator_id = ?",
                                    (ev_id, owner_id),
                                )
                                conn2.commit()
                            finally:
                                conn2.close()
                            await reset_inter.response.edit_message(
                                content=bi_text("✅ تم إعادة اللون الافتراضي.", "✅ Reset to default color."),
                                view=None,
                            )

                        reset_btn = discord.ui.Button(
                            label=bi_text("🔄 اللون الافتراضي | Default Color", "🔄 Default Color"),
                            style=discord.ButtonStyle.danger,
                            row=2,
                        )
                        reset_btn.callback = reset_color
                        self.add_item(reset_btn)

                await inter.response.edit_message(
                    content=bi_text("اختر لون الإمبد لهذا التذكير:", "Choose embed color for this reminder:"),
                    view=ColorSelectView(),
                )

            @discord.ui.button(label="🔔 نوع التنبيه | Ping", style=discord.ButtonStyle.secondary, row=1)
            async def ping_type_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                ev_id = self.selected_event["id"]

                class PingTypeView(discord.ui.View):
                    def __init__(self) -> None:
                        super().__init__(timeout=300)

                    async def interaction_check(self, ping_inter: discord.Interaction) -> bool:
                        if ping_inter.user.id != owner_id:
                            await ping_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return False
                        return True

                    async def _set_ping(self, ping_inter: discord.Interaction, ptype: str, label: str) -> None:
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE events SET ping_type = ? WHERE id = ? AND creator_id = ?",
                                (ptype, ev_id, owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await ping_inter.response.edit_message(
                            content=f"✅ {bi_text('نوع التنبيه', 'Ping type')}: **{label}**",
                            view=None,
                        )

                    @discord.ui.button(label="@everyone", style=discord.ButtonStyle.primary)
                    async def everyone_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                        await self._set_ping(pi, "everyone", "@everyone")

                    @discord.ui.button(label="@here", style=discord.ButtonStyle.secondary)
                    async def here_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                        await self._set_ping(pi, "here", "@here")

                    @discord.ui.button(label=bi_text("الرول | Role", "Role"), style=discord.ButtonStyle.secondary)
                    async def role_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                        await self._set_ping(pi, "role", bi_text("الرول", "Role"))

                    @discord.ui.button(label=bi_text("صامت | None", "None"), style=discord.ButtonStyle.danger)
                    async def none_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                        await self._set_ping(pi, "none", bi_text("صامت", "None"))

                await inter.response.edit_message(
                    content=bi_text("اختر نوع التنبيه لهذا التذكير:", "Choose ping type for this reminder:"),
                    view=PingTypeView(),
                )

            @discord.ui.button(label="📋 تكرار | Duplicate", style=discord.ButtonStyle.secondary, row=1)
            async def duplicate_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                src = self.selected_event
                conn2 = get_conn()
                try:
                    cursor = conn2.execute(
                        """
                        INSERT INTO events (
                            guild_id, creator_id, title, time, days,
                            remind_before_minutes, message, image_url, channel_id,
                            created_at, is_paused
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            src["guild_id"], src["creator_id"],
                            f"{src['title']} (copy)", src["time"], src["days"],
                            int(src["remind_before_minutes"] or 10),
                            src["message"], src["image_url"], src["channel_id"],
                            dt.datetime.now().isoformat(),
                        ),
                    )
                    conn2.commit()
                    new_id = cursor.lastrowid
                finally:
                    conn2.close()

                await inter.response.send_message(
                    f"✅ {bi_text('تم نسخ التذكير', 'Reminder duplicated')}. {bi_text('المعرّف الجديد', 'New ID')}: **{new_id}**",
                    ephemeral=True,
                )

            @discord.ui.button(label="🗑️ حذف التذكير | Delete", style=discord.ButtonStyle.danger, row=2)
            async def delete_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                conn2 = get_conn()
                try:
                    conn2.execute(
                        "DELETE FROM events WHERE id = ? AND creator_id = ?",
                        (self.selected_event["id"], owner_id),
                    )
                    conn2.commit()
                finally:
                    conn2.close()

                await inter.response.edit_message(
                    content=f"{bi_text('✅ تم حذف التذكير', '✅ Reminder deleted')}: **{self.selected_event['title']}**",
                    view=None,
                )

            @discord.ui.button(label="🔙 رجوع للقائمة | Back", style=discord.ButtonStyle.secondary, row=2)
            async def back_to_list_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.edit_message(
                    content=f"📋 **{title}**\n{bi_text('اختر تذكيراً لإدارته:', 'Choose a reminder to manage:')}",
                    view=EventsPickerView(self.all_rows),
                )

        class EventsPickerView(discord.ui.View):
            def __init__(self, rows_data: list[sqlite3.Row]) -> None:
                super().__init__(timeout=300)
                for row in rows_data[:25]:
                    img_mark = "🖼️" if row["image_url"] else "📄"
                    btn = discord.ui.Button(
                        label=f"{img_mark} {row['title'][:40]}",
                        style=discord.ButtonStyle.secondary,
                    )

                    async def on_pick(pick_inter: discord.Interaction, selected=row) -> None:
                        if pick_inter.user.id != owner_id:
                            await pick_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return

                        days_str = format_days_summary(str(selected["days"] or ""))
                        channel_str = (
                            f"<#{selected['channel_id']}>" if selected["channel_id"] else "القناة الافتراضية"
                        )
                        summary = (
                            f"**{selected['title']}**\n"
                            f"🆔 {selected['id']}\n"
                            f"⏰ {selected['time']} | -{selected['remind_before_minutes']} {bi_text('دقيقة', 'minutes')}\n"
                            f"📅 {bi_text('الأيام', 'Days')}: {days_str}\n"
                            f"📢 {channel_str}\n"
                            f"{bi_text('🖼️ يوجد صورة', '🖼️ Has image') if selected['image_url'] else bi_text('📄 بدون صورة', '📄 No image')}"
                        )
                        await pick_inter.response.edit_message(
                            content=summary,
                            view=EventActionsView(selected, rows_data),
                        )

                    btn.callback = on_pick
                    self.add_item(btn)

        await interaction.response.send_message(
            content=f"📋 **{title}**\n{bi_text('اختر تذكيراً لإدارته:', 'Choose a reminder to manage:')}",
            view=EventsPickerView(events_rows),
            ephemeral=True,
        )

    @discord.ui.button(label="➕ إضافة موعد فعالية | Add Event Slot", style=discord.ButtonStyle.success, row=0)
    async def event_slot_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        owner_id = self.owner_id
        guild_id = self.guild_id

        class ReminderSlotView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != owner_id:
                    await inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="✨ إضافة تذكير | Add Reminder", style=discord.ButtonStyle.success, row=0)
            async def create_reminder_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.send_modal(CreateEventModal())

            @discord.ui.button(label="✏️ تعديل تذكير | Edit Reminder", style=discord.ButtonStyle.primary, row=0)
            async def edit_reminder_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                wrapper = RemindersView(owner_id, guild_id)
                await wrapper._show_events_manager(inter, title="تعديل التذكيرات")

            @discord.ui.button(label="📋 عرض التذكيرات | List Reminders", style=discord.ButtonStyle.secondary, row=0)
            async def list_reminders_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                wrapper = RemindersView(owner_id, guild_id)
                await wrapper._show_events_manager(inter, title="عرض التذكيرات")

            @discord.ui.button(label="🔙 رجوع | Back", style=discord.ButtonStyle.secondary, row=1)
            async def back_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                await inter.response.edit_message(
                    content=f"📋 **{bi_text('قسم التذكيرات المتقدم', 'Advanced Reminders Section')}**\n{bi_text('اختر المهمة المطلوبة:', 'Choose the required task:')}",
                    view=RemindersView(owner_id, guild_id),
                )

        await interaction.response.edit_message(
            content=f"🗂️ **{bi_text('إضافة موعد فعالية', 'Add Event Slot')}**\n{bi_text('اختر العملية التي تريد تنفيذها:', 'Choose the operation you want to run:')}",
            view=ReminderSlotView(),
        )

    @discord.ui.button(label="🔧 إعدادات افتراضية | Defaults", style=discord.ButtonStyle.secondary, row=0)
    async def defaults_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
            return

        owner_id = self.owner_id
        guild_id = self.guild_id
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_defaults WHERE user_id = ? AND guild_id = ?",
                (owner_id, guild_id),
            ).fetchone()
        finally:
            conn.close()

        current_time = row["default_time"] if row and row["default_time"] else "غير محددة"
        current_days = format_days_summary(row["default_days"]) if row and row["default_days"] else "غير محددة"
        current_ch = f"<#{row['default_channel_id']}>" if row and row["default_channel_id"] else "القناة الافتراضية للسيرفر"
        current_remind = row["default_remind_before"] if row else 10

        class DefaultsView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != owner_id:
                    await inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                    return False
                return True

            @discord.ui.button(label="⏰ تغيير الوقت الافتراضي", style=discord.ButtonStyle.primary)
            async def set_time(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                class TimeModal(discord.ui.Modal, title="الوقت الافتراضي"):
                    time_input = discord.ui.TextInput(
                        label="الوقت (00:00 - 24:00)",
                        placeholder="مثال: 08:00",
                        max_length=5,
                    )

                    async def on_submit(self, m_inter: discord.Interaction) -> None:
                        t_val = self.time_input.value.strip()
                        if t_val not in TIMES:
                            await m_inter.response.send_message(bi_text("وقت غير صحيح.", "Invalid time value."), ephemeral=True)
                            return
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                """INSERT INTO user_defaults (user_id, guild_id, default_time)
                                   VALUES (?, ?, ?)
                                   ON CONFLICT(user_id, guild_id) DO UPDATE SET default_time = excluded.default_time""",
                                (owner_id, guild_id, t_val),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await m_inter.response.send_message(
                            f"✅ {bi_text('الوقت الافتراضي', 'Default time')}: **{t_val}**",
                            ephemeral=True,
                        )

                await inter.response.send_modal(TimeModal())

            @discord.ui.button(label="📅 تغيير الأيام الافتراضية", style=discord.ButtonStyle.secondary)
            async def set_days(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                async def on_days_picked(days_inter: discord.Interaction, days: str) -> None:
                    conn2 = get_conn()
                    try:
                        conn2.execute(
                            """INSERT INTO user_defaults (user_id, guild_id, default_days)
                               VALUES (?, ?, ?)
                               ON CONFLICT(user_id, guild_id) DO UPDATE SET default_days = excluded.default_days""",
                            (owner_id, guild_id, days),
                        )
                        conn2.commit()
                    finally:
                        conn2.close()
                    await days_inter.response.edit_message(
                        content=f"✅ الأيام الافتراضية: **{format_days_summary(days)}**",
                        view=None,
                    )

                await inter.response.edit_message(
                    content=bi_text("اختر الأيام الافتراضية:", "Choose default days:"),
                    view=DaysSelectView(on_days_picked, owner_id, include_alt_start=False),
                )

            @discord.ui.button(label="📢 تغيير القناة الافتراضية", style=discord.ButtonStyle.secondary)
            async def set_channel(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                if not inter.guild:
                    await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
                    return
                text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)

                class ChSelect(discord.ui.Select):
                    def __init__(self) -> None:
                        opts = [discord.SelectOption(label="القناة الافتراضية للسيرفر", value="default")]
                        opts.extend(
                            discord.SelectOption(label=f"#{ch.name}", value=str(ch.id))
                            for ch in text_channels[:24]
                        )
                        super().__init__(placeholder="اختر القناة", options=opts)

                    async def callback(self, sel_inter: discord.Interaction) -> None:
                        ch_id = None if self.values[0] == "default" else int(self.values[0])
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                """INSERT INTO user_defaults (user_id, guild_id, default_channel_id)
                                   VALUES (?, ?, ?)
                                   ON CONFLICT(user_id, guild_id) DO UPDATE SET default_channel_id = excluded.default_channel_id""",
                                (owner_id, guild_id, ch_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        label = f"<#{ch_id}>" if ch_id else bi_text("القناة الافتراضية للسيرفر", "Server default channel")
                        await sel_inter.response.edit_message(
                            content=f"✅ {bi_text('القناة الافتراضية', 'Default channel')}: {label}",
                            view=None,
                        )

                class ChView(discord.ui.View):
                    def __init__(self) -> None:
                        super().__init__(timeout=300)
                        self.add_item(ChSelect())

                await inter.response.edit_message(
                    content=bi_text("اختر القناة الافتراضية:", "Choose default channel:"),
                    view=ChView(),
                )

            @discord.ui.button(label="🔁 تغيير وقت التنبيه", style=discord.ButtonStyle.secondary)
            async def set_remind(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
                async def on_minutes(mins_inter: discord.Interaction, minutes: int) -> None:
                    conn2 = get_conn()
                    try:
                        conn2.execute(
                            """INSERT INTO user_defaults (user_id, guild_id, default_remind_before)
                               VALUES (?, ?, ?)
                               ON CONFLICT(user_id, guild_id) DO UPDATE SET default_remind_before = excluded.default_remind_before""",
                            (owner_id, guild_id, minutes),
                        )
                        conn2.commit()
                    finally:
                        conn2.close()
                    await mins_inter.response.edit_message(
                        content=f"✅ {bi_text('وقت التنبيه الافتراضي', 'Default reminder lead time')}: **{minutes} {bi_text('دقيقة قبل', 'minutes before')}**",
                        view=None,
                    )

                await inter.response.edit_message(
                    content=bi_text("اختر وقت التنبيه الافتراضي:", "Choose default reminder lead time:"),
                    view=ReminderMinutesSelectView(on_minutes, owner_id),
                )

        await interaction.response.send_message(
            f"**🔧 {bi_text('إعداداتك الافتراضية', 'Your defaults')}**\n"
            f"⏰ {bi_text('الوقت', 'Time')}: `{current_time}`\n"
            f"📅 {bi_text('الأيام', 'Days')}: `{current_days}`\n"
            f"📢 {bi_text('القناة', 'Channel')}: {current_ch}\n"
            f"🔔 {bi_text('التنبيه', 'Reminder')}: `{current_remind} {bi_text('دقيقة قبل', 'minutes before')}`\n\n"
            f"_{bi_text('سيتم تطبيق هذه القيم تلقائياً عند إنشاء تذكير جديد', 'These values are auto-applied when creating new reminders')}_",
            view=DefaultsView(),
            ephemeral=True,
        )

    @discord.ui.button(label="📋 عرض التذكيرات | List", style=discord.ButtonStyle.secondary, row=1)
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_events_manager(interaction, title="عرض التذكيرات")

    @discord.ui.button(label="✏️ تعديل تذكير | Edit", style=discord.ButtonStyle.primary, row=1)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_events_manager(interaction, title="تعديل التذكيرات")

    @discord.ui.button(label="🗑️ حذف تذكير | Delete", style=discord.ButtonStyle.danger, row=2)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
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
            await interaction.response.send_message(bi_text("لا توجد تذكيرات.", "No reminders found."), ephemeral=True)
            return

        owner_id = interaction.user.id

        class DeletePickerView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

                def make_delete_cb(event_id: int, event_title: str):
                    async def delete_cb(del_inter: discord.Interaction) -> None:
                        if del_inter.user.id != owner_id:
                            await del_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "DELETE FROM events WHERE id = ? AND creator_id = ?",
                                (event_id, owner_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await del_inter.response.edit_message(
                            content=f"{bi_text('✅ تم حذف', '✅ Deleted')}: **{event_title}**",
                            view=None,
                        )
                    return delete_cb

                for row in rows[:25]:
                    btn = discord.ui.Button(
                        label=f"🗑️ {row['title'][:28]}",
                        style=discord.ButtonStyle.danger,
                    )
                    btn.callback = make_delete_cb(row["id"], row["title"])
                    self.add_item(btn)

        await interaction.response.send_message(
            bi_text("اختر التذكير للحذف:", "Choose a reminder to delete:"),
            view=DeletePickerView(),
            ephemeral=True,
        )

    @discord.ui.button(label="🔙 رجوع للرئيسية | Home", style=discord.ButtonStyle.secondary, row=2)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"🎮 **{bi_text('لوحة التحكم', 'Control Panel')}**\n{bi_text('اختر قسماً:', 'Choose a section:')}",
            view=PanelHomeView(self.owner_id, self.guild_id),
        )


class SettingsView(discord.ui.View):
    """قسم الإعدادات داخل لوحة التحكم"""

    def __init__(self, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(bi_text("ليس لديك صلاحية.", "You do not have permission."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ إضافة مشرف | Add Admin", style=discord.ButtonStyle.primary, row=0)
    async def add_admin_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        guild_id = self.guild_id

        class AddAdminModal(discord.ui.Modal, title="إضافة مشرف"):
            def __init__(self) -> None:
                super().__init__(timeout=300)
                self.user_input = discord.ui.TextInput(
                    label="User ID",
                    placeholder="123456789012345678",
                    max_length=25,
                )
                self.add_item(self.user_input)

            async def on_submit(self, modal_inter: discord.Interaction) -> None:
                raw = self.user_input.value.strip()
                if not raw.isdigit():
                    await modal_inter.response.send_message(bi_text("معرّف المستخدم غير صحيح.", "Invalid user ID."), ephemeral=True)
                    return
                user_id = int(raw)
                conn = get_conn()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO admins (guild_id, user_id) VALUES (?, ?)",
                        (guild_id, user_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                await modal_inter.response.send_message(
                    f"✅ {bi_text('تمت إضافة', 'Added')} <@{user_id}> {bi_text('كمشرف', 'as admin') }.",
                    ephemeral=True,
                )

        await inter.response.send_modal(AddAdminModal())

    @discord.ui.button(label="👥 المشرفون | Admins", style=discord.ButtonStyle.secondary, row=0)
    async def admins_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        conn = get_conn()
        try:
            admins = conn.execute(
                "SELECT user_id FROM admins WHERE guild_id = ? ORDER BY user_id ASC",
                (self.guild_id,),
            ).fetchall()
        finally:
            conn.close()

        admin_list = (
            "\n".join(f"• <@{a['user_id']}> (`{a['user_id']}`)" for a in admins[:30])
            if admins
            else bi_text("لا يوجد مشرفون مضافون.", "No admins added.")
        )
        guild_id = self.guild_id

        class ManageAdminsView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

            @discord.ui.button(label="➕ إضافة", style=discord.ButtonStyle.success)
            async def add_btn(self, add_inter: discord.Interaction, b: discord.ui.Button) -> None:
                class AddModal(discord.ui.Modal, title="إضافة مشرف"):
                    def __init__(self) -> None:
                        super().__init__(timeout=300)
                        self.user_input = discord.ui.TextInput(label="User ID", max_length=25)
                        self.add_item(self.user_input)

                    async def on_submit(self, modal_inter: discord.Interaction) -> None:
                        raw = self.user_input.value.strip()
                        if not raw.isdigit():
                            await modal_inter.response.send_message(bi_text("معرّف المستخدم غير صحيح.", "Invalid user ID."), ephemeral=True)
                            return
                        user_id = int(raw)
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "INSERT OR IGNORE INTO admins (guild_id, user_id) VALUES (?, ?)",
                                (guild_id, user_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await modal_inter.response.send_message(f"✅ {bi_text('تمت إضافة', 'Added')} <@{user_id}>.", ephemeral=True)

                await add_inter.response.send_modal(AddModal())

            @discord.ui.button(label="❌ حذف مشرف", style=discord.ButtonStyle.danger)
            async def remove_btn(self, rm_inter: discord.Interaction, b: discord.ui.Button) -> None:
                class RemoveModal(discord.ui.Modal, title="حذف مشرف"):
                    def __init__(self) -> None:
                        super().__init__(timeout=300)
                        self.user_input = discord.ui.TextInput(label="User ID", max_length=25)
                        self.add_item(self.user_input)

                    async def on_submit(self, modal_inter: discord.Interaction) -> None:
                        raw = self.user_input.value.strip()
                        if not raw.isdigit():
                            await modal_inter.response.send_message(bi_text("معرّف المستخدم غير صحيح.", "Invalid user ID."), ephemeral=True)
                            return
                        user_id = int(raw)
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "DELETE FROM admins WHERE guild_id = ? AND user_id = ?",
                                (guild_id, user_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await modal_inter.response.send_message(f"✅ {bi_text('تم حذف', 'Removed')} <@{user_id}>.", ephemeral=True)

                await rm_inter.response.send_modal(RemoveModal())

        await inter.response.send_message(
            f"**{bi_text('المشرفون', 'Admins')} ({len(admins)}):**\n{admin_list}",
            view=ManageAdminsView(),
            ephemeral=True,
        )

    @discord.ui.button(label="🖼️ صور التذكيرات | Reminder Images", style=discord.ButtonStyle.secondary, row=0)
    async def images_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild:
            await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
            return
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, title, image_url FROM events WHERE creator_id = ? AND guild_id = ? ORDER BY time ASC",
                (inter.user.id, inter.guild.id),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await inter.response.send_message(bi_text("لا توجد تذكيرات.", "No reminders found."), ephemeral=True)
            return

        owner_id = self.owner_id

        class ImagePickerView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

                def make_img_cb(selected_row):
                    async def img_cb(pick_inter: discord.Interaction) -> None:
                        if pick_inter.user.id != owner_id:
                            await pick_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                            return

                        attachment = await request_image_attachment(pick_inter, owner_id)
                        if not attachment:
                            return

                        class ConfirmImageView(discord.ui.View):
                            def __init__(self) -> None:
                                super().__init__(timeout=60)

                            @discord.ui.button(label="✅ تأكيد الحفظ", style=discord.ButtonStyle.success)
                            async def confirm_image(self, conf_inter: discord.Interaction, button2: discord.ui.Button) -> None:
                                if conf_inter.user.id != owner_id:
                                    await conf_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                                    return
                                conn2 = get_conn()
                                try:
                                    conn2.execute(
                                        "UPDATE events SET image_url = ? WHERE id = ? AND creator_id = ?",
                                        (attachment.url, selected_row["id"], owner_id),
                                    )
                                    conn2.commit()
                                finally:
                                    conn2.close()
                                await conf_inter.response.edit_message(
                                    content=f"✅ {bi_text('تم تحديث الصورة', 'Image updated')}: **{selected_row['title']}**",
                                    view=None,
                                )

                            @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
                            async def cancel_image(self, conf_inter: discord.Interaction, button2: discord.ui.Button) -> None:
                                if conf_inter.user.id != owner_id:
                                    await conf_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                                    return
                                await conf_inter.response.edit_message(content=bi_text("تم إلغاء التحديث.", "Update cancelled."), view=None)

                        await pick_inter.followup.send(
                            f"📸 {bi_text('تم اختيار', 'Selected')}: {attachment.filename}\n{bi_text('هل تريد حفظها للتذكير', 'Do you want to save it for reminder')} **{selected_row['title']}**؟",
                            view=ConfirmImageView(),
                            ephemeral=True,
                        )
                    return img_cb

                for row in rows[:25]:
                    has_img = "🖼️" if row["image_url"] else "📄"
                    b = discord.ui.Button(
                        label=f"{has_img} {row['title'][:26]}",
                        style=discord.ButtonStyle.secondary,
                    )
                    b.callback = make_img_cb(row)
                    self.add_item(b)

        await inter.response.send_message(
            bi_text("اختر التذكير لإضافة/تحديث صورته:", "Choose a reminder to add/update its image:"),
            view=ImagePickerView(),
            ephemeral=True,
        )

    @discord.ui.button(label="📝 تسجيل السيرفر | Register Server", style=discord.ButtonStyle.success, row=0)
    async def register_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild:
            await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
            return
        synced = register_current_server(inter.guild, inter.user.id)
        await inter.response.send_message(
            f"✅ {bi_text('تم تسجيل السيرفر وتحديث', 'Server registered and synced')} {synced} {bi_text('قناة', 'channels') }.",
            ephemeral=True,
        )

    @discord.ui.button(label="📚 عرض القنوات | Show Channels", style=discord.ButtonStyle.secondary, row=0)
    async def channels_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild:
            await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
            return
        rows = get_registered_server_channels(inter.guild.id, only_active=False)
        if rows:
            lines = [f"**{bi_text('قنوات السيرفر المسجلة', 'Registered server channels')} ({len(rows)}):**"]
            for row in rows[:30]:
                status = "✅" if int(row["is_active"]) == 1 else "❌"
                lines.append(f"{status} #{row['channel_name']} (`{row['channel_id']}`)")
            if len(rows) > 30:
                lines.append(f"... +{len(rows) - 30} {bi_text('أخرى', 'more')}")
        else:
            text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)
            lines = [f"**{bi_text('قنوات السيرفر', 'Server channels')} ({len(text_channels)}):**"]
            lines.extend(f"• #{ch.name} (`{ch.id}`)" for ch in text_channels[:30])
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="🔄 تحديث البوت | Update Bot", style=discord.ButtonStyle.primary, row=1)
    async def update_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not is_bot_owner(inter.user.id):
            await inter.response.send_message(bi_text("هذا الخيار لمالك البوت فقط.", "This option is for bot owner only."), ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        bot_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=bot_dir,
        )
        out, err = await proc.communicate()
        text = (out.decode().strip() or err.decode().strip())[:900]
        if proc.returncode != 0:
            await inter.followup.send(f"{bi_text('فشل التحديث', 'Update failed')}:\n```\n{text}\n```", ephemeral=True)
            return
        await inter.followup.send(f"✅ {bi_text('تم التحديث', 'Updated')}:\n```\n{text}\n```", ephemeral=True)

    @discord.ui.button(label="⬆️ ترقية البوت | Upgrade Bot", style=discord.ButtonStyle.success, row=1)
    async def upgrade_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not is_bot_owner(inter.user.id):
            await inter.response.send_message(bi_text("هذا الخيار لمالك البوت فقط.", "This option is for bot owner only."), ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        bot_dir = os.path.dirname(os.path.abspath(__file__)) or os.getcwd()
        git_proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=bot_dir,
        )
        git_out, git_err = await git_proc.communicate()
        git_text = (git_out.decode().strip() or git_err.decode().strip())[:500]
        if git_proc.returncode != 0:
            await inter.followup.send(f"{bi_text('فشل git pull', 'git pull failed')}:\n```\n{git_text}\n```", ephemeral=True)
            return
        pip_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=bot_dir,
        )
        pip_out, pip_err = await pip_proc.communicate()
        pip_text = (pip_out.decode().strip() or pip_err.decode().strip())[-500:]
        if pip_proc.returncode != 0:
            await inter.followup.send(f"{bi_text('فشل pip install', 'pip install failed')}:\n```\n{pip_text}\n```", ephemeral=True)
            return
        await inter.followup.send(
            f"✅ {bi_text('تمت الترقية', 'Upgraded')}.\nGit:\n```\n{git_text}\n```\nPip:\n```\n{pip_text}\n```",
            ephemeral=True,
        )

    @discord.ui.button(label="ℹ️ عن البوت | About", style=discord.ButtonStyle.secondary, row=1)
    async def about_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        await inter.response.send_message(
            f"**{bi_text('حقوق صانع البوت', 'Bot creator credits')}:**\n"
            "```\n"
            "DANGER TNT\n"
            "DC = DANGER_600\n"
            ":$\n"
            "```",
            ephemeral=True,
        )

    @discord.ui.button(label="📞 التواصل مع الدعم | Support", style=discord.ButtonStyle.secondary, row=1)
    async def support_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        support_view = discord.ui.View(timeout=60)
        support_view.add_item(discord.ui.Button(
            label="فتح حساب DANGER_600 في Discord",
            style=discord.ButtonStyle.link,
            url=f"https://discord.com/users/{BOT_OWNER_ID}",
        ))
        await inter.response.send_message(
            f"📞 **{bi_text('التواصل مع الدعم', 'Support contact')}:**\n{bi_text('اضغط على الزر أدناه للتواصل مع DANGER_600', 'Press the button below to contact DANGER_600')}",
            view=support_view,
            ephemeral=True,
        )

    @discord.ui.button(label="🔔 رول التنبيهات | Notification Role", style=discord.ButtonStyle.primary, row=2)
    async def notif_role_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
            await inter.response.send_message(bi_text("للمشرفين فقط.", "Admins only."), ephemeral=True)
            return

        guild_id = self.guild_id

        class RoleSelectView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)
                role_select = discord.ui.RoleSelect(
                    placeholder=bi_text("اختر الرول | Select Role", "Select notification role"),
                    min_values=0,
                    max_values=1,
                )

                async def role_callback(rs_inter: discord.Interaction) -> None:
                    if rs_inter.user.id != inter.user.id:
                        await rs_inter.response.send_message(bi_text("ليس لك.", "Not for you."), ephemeral=True)
                        return
                    role_id = (rs_inter.data.get("values") or [None])[0]
                    conn2 = get_conn()
                    try:
                        conn2.execute(
                            "UPDATE server_settings SET notification_role_id = ? WHERE guild_id = ?",
                            (int(role_id) if role_id else None, guild_id),
                        )
                        conn2.commit()
                    finally:
                        conn2.close()
                    label = f"<@&{role_id}>" if role_id else bi_text("تم مسح الرول", "Role cleared")
                    await rs_inter.response.edit_message(
                        content=f"✅ {bi_text('رول التنبيهات', 'Notification role')}: {label}",
                        view=None,
                    )

                role_select.callback = role_callback
                self.add_item(role_select)

        await inter.response.send_message(
            bi_text("اختر رول التنبيهات للسيرفر:", "Choose notification role for the server:"),
            view=RoleSelectView(),
            ephemeral=True,
        )

    @discord.ui.button(label="🎨 لون الإمبد الافتراضي | Default Color", style=discord.ButtonStyle.secondary, row=2)
    async def default_color_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
            await inter.response.send_message(bi_text("للمشرفين فقط.", "Admins only."), ephemeral=True)
            return

        guild_id = self.guild_id

        class DefColorView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

                def make_color_cb(hex_val: str, color_label: str):
                    async def color_cb(color_inter: discord.Interaction) -> None:
                        conn2 = get_conn()
                        try:
                            conn2.execute(
                                "UPDATE server_settings SET default_embed_color = ? WHERE guild_id = ?",
                                (hex_val, guild_id),
                            )
                            conn2.commit()
                        finally:
                            conn2.close()
                        await color_inter.response.edit_message(
                            content=f"✅ {bi_text('اللون الافتراضي للسيرفر', 'Server default color')}: {color_label}",
                            view=None,
                        )
                    return color_cb

                for idx, (clabel, chex, cemoji) in enumerate(COLOR_PRESETS):
                    cb = discord.ui.Button(label=f"{cemoji} {clabel}", style=discord.ButtonStyle.secondary, row=idx // 5)
                    cb.callback = make_color_cb(chex, clabel)
                    self.add_item(cb)

        await inter.response.send_message(
            bi_text("اختر اللون الافتراضي للسيرفر:", "Choose server default embed color:"),
            view=DefColorView(),
            ephemeral=True,
        )

    @discord.ui.button(label="🔔 نوع التنبيه الافتراضي | Default Ping", style=discord.ButtonStyle.secondary, row=2)
    async def default_ping_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
            await inter.response.send_message(bi_text("للمشرفين فقط.", "Admins only."), ephemeral=True)
            return

        guild_id = self.guild_id

        class DefPingView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=300)

            async def _set_ping(self, pi: discord.Interaction, ptype: str, label: str) -> None:
                conn2 = get_conn()
                try:
                    conn2.execute(
                        "UPDATE server_settings SET default_ping_type = ? WHERE guild_id = ?",
                        (ptype, guild_id),
                    )
                    conn2.commit()
                finally:
                    conn2.close()
                await pi.response.edit_message(
                    content=f"✅ {bi_text('نوع التنبيه الافتراضي', 'Default ping type')}: **{label}**",
                    view=None,
                )

            @discord.ui.button(label="@everyone", style=discord.ButtonStyle.primary)
            async def ev_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                await self._set_ping(pi, "everyone", "@everyone")

            @discord.ui.button(label="@here", style=discord.ButtonStyle.secondary)
            async def here_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                await self._set_ping(pi, "here", "@here")

            @discord.ui.button(label=bi_text("الرول | Role", "Role"), style=discord.ButtonStyle.secondary)
            async def role_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                await self._set_ping(pi, "role", bi_text("الرول", "Role"))

            @discord.ui.button(label=bi_text("صامت | None", "None"), style=discord.ButtonStyle.danger)
            async def none_btn(self, pi: discord.Interaction, b: discord.ui.Button) -> None:
                await self._set_ping(pi, "none", bi_text("صامت", "None"))

        await inter.response.send_message(
            bi_text("اختر نوع التنبيه الافتراضي للسيرفر:", "Choose server default ping type:"),
            view=DefPingView(),
            ephemeral=True,
        )

    @discord.ui.button(label="📊 إحصائيات | Statistics", style=discord.ButtonStyle.secondary, row=2)
    async def stats_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        if not inter.guild or not has_guild_admin_access(inter.guild.id, inter.user.id, inter.guild.owner_id):
            await inter.response.send_message(bi_text("للمشرفين فقط.", "Admins only."), ephemeral=True)
            return

        conn2 = get_conn()
        try:
            total_reminders = conn2.execute(
                "SELECT COUNT(*) as cnt FROM events WHERE guild_id = ?",
                (inter.guild.id,),
            ).fetchone()["cnt"]
            total_admins = conn2.execute(
                "SELECT COUNT(*) as cnt FROM admins WHERE guild_id = ?",
                (inter.guild.id,),
            ).fetchone()["cnt"]
            is_reg = bool(conn2.execute(
                "SELECT 1 FROM registered_servers WHERE guild_id = ?",
                (inter.guild.id,),
            ).fetchone())
        finally:
            conn2.close()

        await inter.response.send_message(
            f"**📊 {bi_text('إحصائيات السيرفر', 'Server Statistics')}:**\n"
            f"📋 {bi_text('التذكيرات الكلية', 'Total reminders')}: **{total_reminders}**\n"
            f"👥 {bi_text('المشرفون', 'Admins')}: **{total_admins}**\n"
            f"✅ {bi_text('مسجل في البوت', 'Registered in bot')}: **{'نعم' if is_reg else 'لا'}**",
            ephemeral=True,
        )

    @discord.ui.button(label="🔙 رجوع للرئيسية | Home", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
        await inter.response.edit_message(
            content=f"🎮 **{bi_text('لوحة التحكم', 'Control Panel')}**\n{bi_text('اختر قسماً:', 'Choose a section:')}",
            view=PanelHomeView(self.owner_id, self.guild_id),
        )


class PanelHomeView(discord.ui.View):
    """الشاشة الرئيسية للوحة التحكم"""

    def __init__(self, owner_id: int, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(bi_text("ليس لديك صلاحية.", "You do not have permission."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📋 التذكيرات | Reminders", style=discord.ButtonStyle.primary, row=0)
    async def reminders_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"📋 **{bi_text('قسم التذكيرات', 'Reminders Section')}**\n{bi_text('اختر ما تريد:', 'Choose what you want:')}",
            view=RemindersView(self.owner_id, self.guild_id),
        )

    @discord.ui.button(label="⚙️ الإعدادات | Settings", style=discord.ButtonStyle.secondary)
    async def settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
            return

        if not has_guild_admin_access(interaction.guild.id, interaction.user.id, interaction.guild.owner_id):
            await interaction.response.send_message(bi_text("للمشرفين فقط.", "Admins only."), ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"⚙️ **{bi_text('قسم الإعدادات', 'Settings Section')}**\n{bi_text('اختر المهمة التي تريد تنفيذها:', 'Choose the task you want to run:')}",
            view=SettingsView(self.owner_id, self.guild_id),
        )


@bot.tree.command(name="panel", description="Open control panel | فتح لوحة التحكم")
async def panel(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
        return

    await interaction.response.send_message(
        f"🎮 {bi_text('لوحة التحكم', 'Control Panel')}",
        view=PanelHomeView(owner_id=interaction.user.id, guild_id=interaction.guild.id),
        ephemeral=True,
    )


@bot.tree.command(name="language", description="Change language | تغيير اللغة")
@app_commands.describe(lang="Language code: en or ar")
@app_commands.choices(
    lang=[
        app_commands.Choice(name="English", value="en"),
        app_commands.Choice(name="العربية", value="ar"),
    ]
)
async def language(interaction: discord.Interaction, lang: app_commands.Choice[str]) -> None:
    set_user_lang(interaction.user.id, lang.value)
    await interaction.response.send_message(
        t(interaction.user.id, "lang_set"),
        ephemeral=True,
    )


@bot.tree.command(name="owner_settings", description="Bot owner settings | إعدادات البوت للمالك")
async def owner_settings(interaction: discord.Interaction) -> None:
    """إعدادات خاصة بمالك البوت فقط"""
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            bi_text("هذا الأمر خاص بمالك البوت فقط.", "This command is for bot owner only."),
            ephemeral=True,
        )
        return

    class OwnerSettingsView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=600)
            self.add_item(
                discord.ui.Button(
                    label="📞 التواصل مع الدعم | Support",
                    style=discord.ButtonStyle.link,
                    url=f"https://discord.com/users/{BOT_OWNER_ID}",
                )
            )

        @discord.ui.button(label="🛠️ إدارة سيرفرات المالك | Owner Servers", style=discord.ButtonStyle.primary)
        async def owner_servers_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            if not inter.guild:
                await inter.response.send_message(bi_text("استخدم هذا الزر داخل سيرفر.", "Use this button inside a server."), ephemeral=True)
                return
            await inter.response.send_message(
                bi_text("لوحة إعدادات المالك للسيرفرات:", "Owner server settings panel:"),
                view=OwnerServerSettingsView(owner_id=inter.user.id, guild_id=inter.guild.id),
                ephemeral=True,
            )

        @discord.ui.button(label="🔄 مزامنة الأوامر | Sync Commands", style=discord.ButtonStyle.success)
        async def sync_commands_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            await inter.response.defer(ephemeral=True, thinking=True)
            synced = await bot.tree.sync()
            await inter.followup.send(
                f"✅ {bi_text('تمت مزامنة', 'Synced')} {len(synced)} {bi_text('أمر', 'commands')}.",
                ephemeral=True,
            )

        @discord.ui.button(label="©️ حقوق صانع البوت | Credits", style=discord.ButtonStyle.secondary)
        async def credits_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            await inter.response.send_message(
                f"{bi_text('حقوق صانع البوت', 'Bot creator credits')}:\n"
                "DANGER TNT\n"
                "DC = DANGER_600\n"
                ":$\n"
                "Support: DANGET_600",
                ephemeral=True,
            )

    await interaction.response.send_message(
        bi_text("⚙️ إعدادات المالك (أمر منفصل):", "⚙️ Owner settings (separate command):"),
        view=OwnerSettingsView(),
        ephemeral=True,
    )


@bot.tree.command(name="setup", description="إعدادات المالك | Owner Setup")
async def setup(interaction: discord.Interaction) -> None:
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            bi_text("هذا الأمر خاص بمالك البوت فقط.", "This command is for bot owner only."),
            ephemeral=True,
        )
        return

    if not interaction.guild:
        await interaction.response.send_message(bi_text("يجب استخدام هذا الأمر داخل سيرفر.", "This command must be used inside a server."), ephemeral=True)
        return

    guild_id = interaction.guild.id

    class SetupView(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=600)
            self.add_item(discord.ui.Button(
                label="📞 التواصل مع الدعم | Support - DANGER_600",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/users/{BOT_OWNER_ID}",
                row=2,
            ))

        @discord.ui.button(label="📝 تسجيل هذا السيرفر | Register Server", style=discord.ButtonStyle.success, row=0)
        async def register_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            if not inter.guild:
                await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
                return
            synced = register_current_server(inter.guild, inter.user.id)
            await inter.response.send_message(
                f"✅ {bi_text('تم تسجيل', 'Registered')} **{inter.guild.name}** {bi_text('وتحديث', 'and synced')} {synced} {bi_text('قناة', 'channels')}.",
                ephemeral=True,
            )

        @discord.ui.button(label="🔄 مزامنة الأوامر | Sync Commands", style=discord.ButtonStyle.secondary, row=0)
        async def sync_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            await inter.response.defer(ephemeral=True, thinking=True)
            synced = await bot.tree.sync()
            await inter.followup.send(
                f"✅ {bi_text('تمت مزامنة', 'Synced')} {len(synced)} {bi_text('أمر', 'commands')}.",
                ephemeral=True,
            )

        @discord.ui.button(label="🛠️ إدارة السيرفرات | Manage Servers", style=discord.ButtonStyle.primary, row=1)
        async def manage_servers_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            if not inter.guild:
                await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
                return
            await inter.response.send_message(
                bi_text("لوحة إدارة السيرفرات:", "Server management panel:"),
                view=OwnerServerSettingsView(owner_id=inter.user.id, guild_id=inter.guild.id),
                ephemeral=True,
            )

        @discord.ui.button(label="📚 عرض القنوات | Show Channels", style=discord.ButtonStyle.secondary, row=1)
        async def channels_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            if not inter.guild:
                await inter.response.send_message(bi_text("للخادم فقط.", "Server only."), ephemeral=True)
                return
            rows = get_registered_server_channels(inter.guild.id, only_active=False)
            if rows:
                lines = [f"**{bi_text('قنوات مسجلة', 'Registered channels')} ({len(rows)}):**"]
                for row in rows[:30]:
                    s = "✅" if int(row["is_active"]) == 1 else "❌"
                    lines.append(f"{s} #{row['channel_name']} (`{row['channel_id']}`)")
            else:
                text_channels = sorted(inter.guild.text_channels, key=lambda c: c.position)
                lines = [f"**{bi_text('قنوات السيرفر', 'Server channels')} ({len(text_channels)}):**"]
                lines.extend(f"• #{ch.name} (`{ch.id}`)" for ch in text_channels[:30])
            await inter.response.send_message("\n".join(lines), ephemeral=True)

        @discord.ui.button(label="ℹ️ حقوق صانع البوت | Credits", style=discord.ButtonStyle.secondary, row=1)
        async def credits_btn(self, inter: discord.Interaction, btn: discord.ui.Button) -> None:
            await inter.response.send_message(
                f"**{bi_text('حقوق صانع البوت', 'Bot creator credits')}:**\n"
                "```\n"
                "DANGER TNT\n"
                "DC = DANGER_600\n"
                ":$\n"
                "```",
                ephemeral=True,
            )

    await interaction.response.send_message(
        f"⚙️ **{bi_text('إعدادات المالك', 'Owner setup')} - {interaction.guild.name}:**",
        view=SetupView(),
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
