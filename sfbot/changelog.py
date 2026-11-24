"""Tell the group what changed when a new version of the bot starts.

The Docker build writes the recent history of main to a file (see the
Dockerfile): one record per commit, newest first, each the hash, subject and
body separated by \\x1f and ended by \\x1e. On startup the bot posts every entry
newer than the last commit it announced.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .leaderboard import LeaderboardStore
from .telegram import TelegramClient

LOG = logging.getLogger(__name__)

_MERGE = re.compile(r"Merge pull request #(\d+) from ")


def read_changelog(path: Path) -> list[tuple[str, str]]:
    """Return (commit, title) pairs, newest first.

    A GitHub merge commit's subject only names the branch, so its title is the
    pull request's, which GitHub puts on the body's first line.
    """

    entries = []
    for record in path.read_text(encoding="utf-8").split("\x1e"):
        # Not str.strip(): it counts the \x1f separators as whitespace.
        record = record.strip("\n")
        if not record:
            continue
        commit, subject, body = record.split("\x1f")
        title = subject.strip()
        merge = _MERGE.match(title)
        if merge is not None:
            lines = [line.strip() for line in body.splitlines() if line.strip()]
            title = f"{lines[0] if lines else title} (#{merge.group(1)})"
        entries.append((commit, title))
    return entries


def changelog_message(entries: list[tuple[str, str]], since: str) -> str:
    """List the titles of every entry newer than the commit since."""

    commits = [commit for commit, _ in entries]
    newer = entries[: commits.index(since)] if since in commits else entries
    lines = ["sfbot has been updated:", *(f"• {title}" for _, title in newer)]
    if since not in commits:
        lines.append("• …and earlier changes")
    return "\n".join(lines)


def announce_changelog(
    client: TelegramClient, store: LeaderboardStore, *, chat_id: int, path: Path
) -> None:
    """Post what changed since the last announced version, once per version.

    The first run only records where the bot is, so a fresh install does not
    post the whole history.
    """

    entries = read_changelog(path)
    if not entries:
        return
    head = entries[0][0]
    since = store.announced_commit(chat_id=chat_id)
    if since != head:
        if since is not None:
            client.send_message(chat_id=chat_id, text=changelog_message(entries, since))
        store.mark_announced(chat_id=chat_id, commit=head)
        LOG.info("Announced version %s", head[:7])
