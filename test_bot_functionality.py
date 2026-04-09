#!/usr/bin/env python
"""
Integration test for Discord bot buttons and modals.
Tests core functionality for all user-facing UI components.
"""

import sys

import bot


def test_times_list():
    """Test the TIMES constant contains correct values."""
    print("Testing TIMES list...")
    assert "00:00" in bot.TIMES, "00:00 should be in TIMES"
    assert "23:30" in bot.TIMES, "23:30 should be in TIMES"
    assert "12:00" in bot.TIMES, "12:00 should be in TIMES"
    assert "24:00" in bot.TIMES, "24:00 should be in TIMES"
    assert len(bot.TIMES) == 49, f"TIMES should have 49 entries, got {len(bot.TIMES)}"
    assert "00:15" not in bot.TIMES, "00:15 should NOT be in TIMES"
    print("✓ TIMES list is correct")


def test_format_days_summary():
    """Test format_days_summary helper."""
    print("Testing format_days_summary...")
    assert bot.format_days_summary("0,1,2,3,4,5,6") == "كل الأيام", "All days should return Arabic label"
    assert "الاثنين" in bot.format_days_summary("0"), "Monday value should return correct Arabic name"
    summary = bot.format_days_summary("0,4")
    assert "الاثنين" in summary and "الجمعة" in summary, "Multiple days summary failed"
    assert bot.format_days_summary("alt") == "يوم إيه / يوم لا", "Alternate days label should be supported"
    assert bot.format_days_summary("") == "-", "Empty days should return '-'"
    print("✓ format_days_summary works")


def test_image_url_validation():
    """Test image URL validation."""
    print("Testing image URL validation...")
    assert bot.validate_image_url(None) is True, "None should be valid"
    assert bot.validate_image_url("") is True, "Empty string should be valid"
    assert bot.validate_image_url("https://example.com/image.png") is True, "HTTPS URL should be valid"
    assert bot.validate_image_url("http://example.com/image.jpg") is True, "HTTP URL should be valid"
    assert bot.validate_image_url("ftp://example.com/image.png") is False, "FTP URL should be invalid"
    assert bot.validate_image_url("not-a-url") is False, "Plain text should be invalid"
    print("✓ Image URL validation works")


def test_user_language_settings():
    """Test user language persistence."""
    print("Testing user language settings...")
    test_user_id = 999_999_001
    conn = bot.get_conn()
    try:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (test_user_id,))
        conn.commit()
    finally:
        conn.close()
    assert bot.get_user_lang(test_user_id) == "en", "Default language should be English"
    bot.set_user_lang(test_user_id, "ar")
    assert bot.get_user_lang(test_user_id) == "ar", "Language should persist to Arabic"
    bot.set_user_lang(test_user_id, "en")
    assert bot.get_user_lang(test_user_id) == "en", "Language should persist to English"
    conn = bot.get_conn()
    try:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (test_user_id,))
        conn.commit()
    finally:
        conn.close()
    print("✓ User language settings work")


def test_translation_function():
    """Test i18n translation function."""
    print("Testing translation function...")
    user_en = 999_999_002
    user_ar = 999_999_003
    bot.set_user_lang(user_en, "en")
    bot.set_user_lang(user_ar, "ar")
    msg_en = bot.t(user_en, "lang_set")
    assert "English" in msg_en or "language" in msg_en.lower(), "English translation failed"
    msg_ar = bot.t(user_ar, "lang_set")
    assert "ع" in msg_ar or "تم" in msg_ar, "Arabic translation failed"
    conn = bot.get_conn()
    try:
        conn.execute("DELETE FROM user_settings WHERE user_id IN (?, ?)", (user_en, user_ar))
        conn.commit()
    finally:
        conn.close()
    print("✓ Translation function works")


def test_event_time_validation():
    """Test TIMES-based event time validation."""
    print("Testing event time validation...")
    valid_times = ["00:00", "00:30", "12:00", "23:30", "24:00"]
    invalid_times = ["00:15", "invalid", "", "1:00", "12:60", "24:30"]
    for time in valid_times:
        assert time in bot.TIMES, f"{time} should be valid"
    for time in invalid_times:
        assert time not in bot.TIMES, f"{time} should be invalid"
    print("✓ Event time validation works")


def test_parse_event_time():
    """Test parsing time strings including 24:00."""
    print("Testing parse_event_time...")
    h, m, add_day = bot.parse_event_time("12:30")
    assert (h, m, add_day) == (12, 30, False), "Normal time parsing failed"
    h, m, add_day = bot.parse_event_time("24:00")
    assert (h, m, add_day) == (0, 0, True), "24:00 parsing should roll to next day"
    print("✓ parse_event_time works")


def test_database_schema():
    """Test database schema is correctly initialized."""
    print("Testing database schema...")
    conn = bot.get_conn()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "events" in tables, "events table not found"
        assert "user_settings" in tables, "user_settings table not found"
        assert "server_settings" in tables, "server_settings table not found"
        assert "admins" in tables, "admins table not found"

        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        expected = {
            "id", "guild_id", "creator_id", "title", "time",
            "days", "remind_before_minutes", "message", "image_url",
            "last_sent_marker", "channel_id", "created_at",
        }
        assert expected.issubset(columns), f"Missing columns: {expected - columns}"
    finally:
        conn.close()
    print("✓ Database schema is correct")


def test_modal_classes_exist():
    """Test that all modal classes are defined and importable."""
    print("Testing modal classes...")
    assert hasattr(bot, "CreateEventModal"), "CreateEventModal not found"
    assert hasattr(bot, "EditMessageModal"), "EditMessageModal not found"
    print("✓ All modal classes exist")


def test_view_classes_exist():
    """Test that all view classes are defined and importable."""
    print("Testing view classes...")
    assert hasattr(bot, "ControlPanelView"), "ControlPanelView not found"
    assert hasattr(bot, "MainPanelView"), "MainPanelView not found"
    assert hasattr(bot, "DaysSelectView"), "DaysSelectView not found"
    assert hasattr(bot, "ReminderMinutesSelectView"), "ReminderMinutesSelectView not found"
    print("✓ All view classes exist")


def test_bot_class_initialization():
    """Test bot class initializes without errors."""
    print("Testing bot class initialization...")
    assert hasattr(bot, "ReminderBot"), "ReminderBot class not found"
    assert hasattr(bot, "bot"), "bot instance not found"
    print("✓ Bot class initializes correctly")


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Starting Discord Bot Functionality Tests")
    print("=" * 60)
    print()

    tests = [
        test_times_list,
        test_format_days_summary,
        test_image_url_validation,
        test_user_language_settings,
        test_translation_function,
        test_event_time_validation,
        test_parse_event_time,
        test_database_schema,
        test_modal_classes_exist,
        test_view_classes_exist,
        test_bot_class_initialization,
    ]

    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"✗ {test.__name__} FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__} ERROR: {e}")
            failed += 1

    print()
    print("=" * 60)
    if failed == 0:
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return 0
    else:
        print(f"✗ {failed} TEST(S) FAILED")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
