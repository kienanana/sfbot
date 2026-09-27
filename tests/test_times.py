import unittest

from sfbot.times import (
    due_post_date,
    due_reminder_date,
    local_date,
    parse_daily_time,
)

SGT_OFFSET = 480


class LocalDateTests(unittest.TestCase):
    def test_the_local_day_boundary_follows_the_offset(self) -> None:
        # 2033-05-18 15:59:59 UTC is 23:59:59 the same day in SGT.
        self.assertEqual(
            local_date(2_000_044_799, utc_offset_minutes=SGT_OFFSET), "2033-05-18"
        )
        self.assertEqual(
            local_date(2_000_044_800, utc_offset_minutes=SGT_OFFSET), "2033-05-19"
        )

    def test_utc_and_local_dates_differ_late_in_the_day(self) -> None:
        self.assertEqual(local_date(2_000_044_800, utc_offset_minutes=0), "2033-05-18")


class DuePostDateTests(unittest.TestCase):
    def test_the_window_opens_at_the_daily_time(self) -> None:
        # 21:30 and 20:30 SGT on 2033-05-18.
        self.assertEqual(
            due_post_date(
                2_000_035_800, utc_offset_minutes=SGT_OFFSET, post_minute=21 * 60
            ),
            "2033-05-18",
        )
        self.assertEqual(
            due_post_date(
                2_000_032_200, utc_offset_minutes=SGT_OFFSET, post_minute=21 * 60
            ),
            "2033-05-17",
        )

    def test_a_missed_window_is_still_due_the_next_morning(self) -> None:
        # 00:10 SGT on 2033-05-19 still owes the 2033-05-18 leaderboard.
        self.assertEqual(
            due_post_date(
                2_000_045_400, utc_offset_minutes=SGT_OFFSET, post_minute=21 * 60
            ),
            "2033-05-18",
        )


class DueReminderDateTests(unittest.TestCase):
    def test_the_window_opens_an_hour_before_the_post(self) -> None:
        # 20:00 and 19:59 SGT on 2033-05-18.
        self.assertEqual(
            due_reminder_date(
                2_000_030_400, utc_offset_minutes=SGT_OFFSET, post_minute=21 * 60
            ),
            "2033-05-18",
        )
        self.assertEqual(
            due_reminder_date(
                2_000_030_340, utc_offset_minutes=SGT_OFFSET, post_minute=21 * 60
            ),
            "2033-05-17",
        )

    def test_a_post_just_after_midnight_is_warned_the_evening_before(self) -> None:
        # 23:45 SGT on 2033-05-18 is the reminder for the 00:30 post on the 19th.
        self.assertEqual(
            due_reminder_date(
                2_000_043_900, utc_offset_minutes=SGT_OFFSET, post_minute=30
            ),
            "2033-05-19",
        )
        # A minute before that window it is still the 18th's own reminder day.
        self.assertEqual(
            due_reminder_date(
                2_000_042_940, utc_offset_minutes=SGT_OFFSET, post_minute=30
            ),
            "2033-05-18",
        )


class ParseDailyTimeTests(unittest.TestCase):
    def test_valid_times_become_minutes_past_midnight(self) -> None:
        self.assertEqual(parse_daily_time("21:00"), 1_260)
        self.assertEqual(parse_daily_time("00:00"), 0)
        self.assertEqual(parse_daily_time("23:59"), 1_439)

    def test_invalid_times_are_rejected(self) -> None:
        for value in ("", "21", "21:60", "24:00", "9pm", "21:0a"):
            with self.assertRaises(ValueError, msg=value):
                parse_daily_time(value)


if __name__ == "__main__":
    unittest.main()
