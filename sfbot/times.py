"""Local-day arithmetic for the daily games leaderboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

REMINDER_LEAD_MINUTES = 60


def local_date(epoch_seconds: int, *, utc_offset_minutes: int) -> str:
    """Return the ISO calendar date an epoch timestamp falls on locally."""

    return _local_datetime(epoch_seconds, utc_offset_minutes).strftime("%Y-%m-%d")


def due_post_date(now: int, *, utc_offset_minutes: int, post_minute: int) -> str:
    """Return the latest local date whose posting window has already opened.

    Before the daily time that is the previous local date, so a window missed
    while the bot was down is still posted on the next poll instead of skipped.
    """

    moment = _local_datetime(now, utc_offset_minutes)
    if moment.hour * 60 + moment.minute < post_minute:
        moment -= timedelta(days=1)
    return moment.strftime("%Y-%m-%d")


def due_reminder_date(now: int, *, utc_offset_minutes: int, post_minute: int) -> str:
    """Return the local date whose reminder window has already opened.

    The reminder runs an hour before that day's post, which for a post shortly
    after local midnight falls on the previous calendar day.
    """

    moment = _local_datetime(now, utc_offset_minutes)
    minutes = moment.hour * 60 + moment.minute
    days = (minutes - (post_minute - REMINDER_LEAD_MINUTES)) // (24 * 60)
    return (moment + timedelta(days=days)).strftime("%Y-%m-%d")


def parse_daily_time(value: str) -> int:
    """Convert an HH:MM local time of day into minutes past local midnight."""

    hours, separator, minutes = value.strip().partition(":")
    if not separator or not hours.isdigit() or not minutes.isdigit():
        raise ValueError("expected HH:MM")

    hour = int(hours)
    minute = int(minutes)
    if hour > 23 or minute > 59:
        raise ValueError("expected a valid time of day")
    return hour * 60 + minute


def _local_datetime(epoch_seconds: int, utc_offset_minutes: int) -> datetime:
    # A fixed offset rather than a named zone: Singapore has observed no DST
    # since 1982, so this needs no tzdata in the slim container image.
    return datetime.fromtimestamp(
        epoch_seconds, timezone(timedelta(minutes=utc_offset_minutes))
    )
