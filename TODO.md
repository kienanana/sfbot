# Production readiness TODO

The project uses one existing Telegram bot and one production container on the
group's homelab. Only one process may poll Telegram with the bot token at a
time. Temporary downtime and loss of the non-critical five-day cache are
acceptable.

## P0 — Developer workflow

- [x] Document the existing-bot prerequisite without a BotFather tutorial.
- [x] Document local and homelab run commands.
- [x] Document the one-bot, single-instance testing workflow.
- [x] Confirm `.env` and `data/` are ignored by Git.
- [x] Add `make run`, `make test`, and `make logs` commands.

## P0 — Runtime reliability

- [ ] Persist the last successfully processed Telegram update ID in SQLite.
- [ ] Prevent a replayed duplicate update from sending a second `sf` response.
- [ ] Isolate failures per update so one malformed message cannot stop polling.
- [ ] Handle `SIGINT` and `SIGTERM` cleanly and close SQLite before exiting.
- [ ] Verify transient Telegram and network failures retry the pending update.
- [ ] Report a clear startup error if a Telegram webhook is already configured.
- [ ] Keep logs useful without exposing the bot token or message text.

## P1 — Automated checks

- [ ] Test polling offsets and restart/replay behavior.
- [ ] Test Telegram API errors and network retry behavior.
- [ ] Test messages containing multiple distinct and repeated tweet links.
- [ ] Test the exact five-day expiration boundary.
- [ ] Test graceful shutdown and database closure.
- [x] Add GitHub Actions to run tests and bytecode compilation.
- [x] Build the Docker image in CI.

## P1 — Homelab deployment

- [x] Choose the group's homelab as the always-on production host.
- [ ] Confirm Docker and Docker Compose are installed on the host.
- [ ] Clone the repository and create the production `.env` securely.
- [ ] Deploy exactly one `sfbot` container.
- [ ] Confirm Docker starts at boot and `restart: unless-stopped` works.
- [ ] Add Docker log rotation and reasonable CPU/memory limits.
- [ ] Perform and document one deployment and rollback.

## P2 — Lightweight operations

- [ ] Add a container health check based on recent successful Telegram polling.
- [ ] Document how to inspect status, logs, and container restart count.
- [ ] Confirm container rebuilds preserve the `sfbot-data` volume.

## P1 — Daily games leaderboard

- [x] Collect results by direct message so the group sees no share text.
- [x] Confirm a DM'd result in the DM, and a group paste with a 👍.
- [x] Serve `/leaderboard` on demand and post automatically at 21:00 SGT.
- [x] Make the first result of a game on a day final.
- [x] Add a Krillion parser from real share text.
- [x] Add a Fermi parser from real share text.
- [x] Add a Connections parser from real share text.
- [x] Restrict submissions to people the bot has seen in the group.
- [x] DM each member their outstanding games an hour before the daily post.
- [x] Decided: no retention window. Results are ~600 KB/year and are the
      durable artifact, unlike the five-day tweet cache.

## Release acceptance test

- [ ] A first Twitter/X link is stored without a response.
- [ ] Sharing the same link again replies `sf` to the original message.
- [ ] Equivalent `twitter.com` and `x.com` URL variants match.
- [ ] Tracking parameters and `/photo/1` suffixes do not affect matching.
- [ ] Different Telegram chats have independent caches.
- [ ] The cache survives a container restart.
- [ ] An expired entry becomes a new origin.
- [ ] A deleted original message promotes the current share safely.
- [ ] The bot recovers after a temporary network failure.
- [ ] The bot starts automatically after the homelab reboots.
- [ ] A Wordle result DM'd to the bot is confirmed and never reaches the group.
- [ ] A second Wordle DM from the same person is refused.
- [ ] Each member can DM the bot after pressing Start.
- [ ] A DM from a non-member is refused and nothing is recorded.
- [ ] Talking in the group is enough to become able to submit by DM.
- [ ] `/leaderboard` lists each game's board, best result first.
- [ ] The daily post arrives once at 21:00 SGT and not again after a restart.
- [ ] Reminders arrive by DM at 20:00 SGT and name only outstanding games.
- [ ] A repeated link's callout names the reposter when an action word is set.
