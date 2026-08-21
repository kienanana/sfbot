"""Twitter/X URL recognition and canonicalization."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit


_VISIBLE_URL = re.compile(
    r"(?i)(?:https?://)?(?:[a-z0-9-]+\.)?(?:twitter\.com|x\.com)/[^\s<>]+"
)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


def tweet_id_from_url(value: str) -> str | None:
    """Return the numeric status ID for a Twitter/X status URL.

    Usernames, host aliases, query parameters, and fragments are deliberately
    ignored: Twitter and X URLs with the same status ID identify the same post.
    """

    candidate = value.strip().rstrip(_TRAILING_PUNCTUATION)
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    if not (
        host in {"twitter.com", "x.com"}
        or host.endswith(".twitter.com")
        or host.endswith(".x.com")
    ):
        return None

    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "status" and parts[index + 1].isdigit():
            return parts[index + 1]
    return None


def _text_link_urls(entities: Iterable[Mapping[str, object]]) -> Iterable[str]:
    for entity in entities:
        if entity.get("type") == "text_link" and isinstance(entity.get("url"), str):
            yield str(entity["url"])


def extract_tweet_ids(message: Mapping[str, object]) -> list[str]:
    """Extract unique tweet IDs from a Telegram Message object.

    Visible URLs are scanned directly, which avoids Telegram's UTF-16 entity
    offset rules. Hidden URLs attached to linked text are read from entities.
    """

    candidates: list[str] = []
    for body_key, entities_key in (("text", "entities"), ("caption", "caption_entities")):
        body = message.get(body_key)
        if isinstance(body, str):
            candidates.extend(match.group(0) for match in _VISIBLE_URL.finditer(body))

        entities = message.get(entities_key)
        if isinstance(entities, list):
            candidates.extend(_text_link_urls(e for e in entities if isinstance(e, Mapping)))

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tweet_id = tweet_id_from_url(candidate)
        if tweet_id is not None and tweet_id not in seen:
            seen.add(tweet_id)
            result.append(tweet_id)
    return result

