"""Minimal Telegram Bot API client and long-polling application."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from .cache import DuplicateCache
from .links import extract_tweet_ids


LOG = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    def __init__(self, status: int, description: str):
        super().__init__(f"Telegram API error {status}: {description}")
        self.status = status
        self.description = description


class TelegramClient:
    def __init__(self, token: str, *, api_root: str = "https://api.telegram.org"):
        self._token = token
        self._base_url = f"{api_root.rstrip('/')}/bot{token}"

    def _redact(self, text: str) -> str:
        if not self._token:
            return text
        return text.replace(self._token, "[REDACTED]")

    def _call(self, method: str, payload: Mapping[str, object], *, timeout: int) -> Any:
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                body = json.load(error)
                description = str(body.get("description", error.reason))
            except (ValueError, AttributeError):
                description = str(error.reason)
            raise TelegramAPIError(error.code, self._redact(description)) from None
        except urllib.error.URLError as error:
            reason = self._redact(str(error.reason))
            raise urllib.error.URLError(reason) from None

        if not result.get("ok"):
            description = self._redact(str(result.get("description", "unknown error")))
            raise TelegramAPIError(
                int(result.get("error_code", 500)), description
            )
        return result.get("result")

    def get_webhook_info(self) -> dict[str, Any]:
        result = self._call("getWebhookInfo", {}, timeout=20)
        return result if isinstance(result, dict) else {}


    def get_updates(self, *, offset: int | None, poll_timeout: int) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=poll_timeout + 10)
        return result if isinstance(result, list) else []

    def send_sf_reply(self, *, chat_id: int, message_id: int) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "sf",
                "reply_parameters": {
                    "message_id": message_id,
                    "allow_sending_without_reply": False,
                },
            },
            timeout=20,
        )


def verify_no_active_webhook(client: TelegramClient) -> None:
    info = client.get_webhook_info()
    url = info.get("url")
    if url:
        raise RuntimeError(
            f"Telegram webhook is currently configured ({url}); "
            "delete the webhook before running sfbot with long polling."
        )


def handle_message(
    message: Mapping[str, object], *, cache: DuplicateCache, client: TelegramClient
) -> None:
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or not isinstance(chat.get("id"), int):
        return
    if not isinstance(message.get("message_id"), int):
        return

    chat_id = int(chat["id"])
    message_id = int(message["message_id"])
    seen_at = int(message.get("date", time.time()))

    for tweet_id in extract_tweet_ids(message):
        if cache.has_replied(chat_id=chat_id, message_id=message_id, tweet_id=tweet_id):
            continue

        original = cache.find_or_record(
            chat_id=chat_id,
            tweet_id=tweet_id,
            message_id=message_id,
            seen_at=seen_at,
        )
        if original is None:
            continue

        try:
            client.send_sf_reply(chat_id=chat_id, message_id=original.message_id)
            cache.record_reply(
                chat_id=chat_id,
                message_id=message_id,
                tweet_id=tweet_id,
                seen_at=seen_at,
            )
        except TelegramAPIError as error:
            description = error.description.lower()
            if error.status == 400 and "repl" in description and "not found" in description:
                cache.replace_origin(
                    chat_id=chat_id,
                    tweet_id=tweet_id,
                    old_message_id=original.message_id,
                    new_message_id=message_id,
                    seen_at=seen_at,
                )
                LOG.info("Stored origin message was deleted; promoted message %s", message_id)
                continue
            if 400 <= error.status < 500 and error.status != 429:
                LOG.error(
                    "Non-retryable Telegram client error %s when replying in chat %s: %s",
                    error.status,
                    chat_id,
                    error.description,
                )
                continue
            raise


def run_polling(
    client: TelegramClient,
    cache: DuplicateCache,
    *,
    poll_timeout: int = 30,
    stop_event: threading.Event | None = None,
) -> None:
    verify_no_active_webhook(client)
    last_update_id = cache.get_last_update_id()
    offset: int | None = (last_update_id + 1) if last_update_id is not None else None
    backoff = 1
    LOG.info("sfbot is listening for messages (starting offset: %s)", offset)

    while stop_event is None or not stop_event.is_set():
        try:
            updates = client.get_updates(offset=offset, poll_timeout=poll_timeout)
            backoff = 1
            for update in updates:
                update_id = update.get("update_id")
                message = update.get("message")
                if isinstance(message, Mapping):
                    try:
                        handle_message(message, cache=cache, client=client)
                    except (TelegramAPIError, urllib.error.URLError, TimeoutError):
                        raise
                    except Exception:
                        LOG.exception("Unexpected error processing update %s; skipping", update_id)

                # Only acknowledge an update after all of its side effects have
                # succeeded. A transient sendMessage failure is then retried.
                if isinstance(update_id, int):
                    offset = update_id + 1
                    cache.set_last_update_id(update_id)
        except (TelegramAPIError, urllib.error.URLError, TimeoutError) as error:
            if stop_event is not None and stop_event.is_set():
                break
            LOG.warning("Telegram request failed (%s); retrying in %ss", error, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except KeyboardInterrupt:
            LOG.info("sfbot received stop signal; exiting polling loop")
            break

