# sfbot

`sfbot` watches a Telegram group for Twitter/X status links. If the same post
was shared in that chat during the previous five days, it sends `sf` as a reply
to the first message so tapping the reply navigates to the original share.

The canonical key is Twitter's numeric status ID. This means `twitter.com` and
`x.com` links, different usernames, mobile subdomains, tracking query strings,
and `/photo/1` suffixes all resolve to the same post. Origins are stored in an
indexed SQLite database, scoped per Telegram chat, and expired automatically.

## Architecture

![sfbot message-processing flow](docs/sfbot-flow.png)

## Set up Telegram

1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, and save the token.
2. In BotFather, run `/setprivacy`, choose the bot, and select **Disable**. This
   is required because the bot needs to see ordinary link messages in a group.
   If the bot was already in the group, remove it and add it again after changing
   privacy mode. Alternatively, making the bot a group admin also lets it see
   all messages.
3. Add the bot to the group. It only needs permission to read and send messages.

Telegram enables privacy mode by default; its implications are documented in
the official [Bot Features guide](https://core.telegram.org/bots/features#privacy-mode).

## Run with Docker Compose

```sh
cp .env.example .env
# Edit .env and insert the BotFather token.
docker compose up -d --build
```

The named `sfbot-data` volume preserves the cache across restarts. View logs
with `docker compose logs -f sfbot`.

## Run with Python

Python 3.11 or newer is enough; the bot has no third-party runtime dependencies.

```sh
export TELEGRAM_BOT_TOKEN='your-token'
python -m sfbot
```

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SFBOT_DB_PATH` | `data/sfbot.db` | SQLite database path |
| `SFBOT_RETENTION_DAYS` | `5` | Duplicate window in whole days |
| `SFBOT_POLL_TIMEOUT` | `30` | Telegram long-poll timeout in seconds |
| `SFBOT_LOG_LEVEL` | `INFO` | Python log level |

Run the test suite with:

```sh
python -m unittest discover -s tests -v
```

## Behavior details

- Deduplication is per group/chat, not global across every group using the bot.
- The earliest share remains the reply target for the full five-day window.
- If that original message was deleted, the current share becomes the new
  origin; the bot avoids sending an `sf` reply with nowhere to navigate.
- Text messages, media captions, and links hidden behind Telegram linked text
  are supported.
