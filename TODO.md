# Project backlog

The bot runs as one process and one Docker container on the group's homelab.
Only one instance may poll with the token at a time. Temporary downtime and loss
of the five-day link cache are acceptable; game results are durable and have no
retention window. Deployment checks below remain unverified by local tests.

## P1 — Reliability and result correctness

- [ ] Persist the processed Telegram update offset in SQLite and test restart
      behavior for commands, submissions, and duplicate replies.
- [ ] Track daily-post delivery per message (board, CHAD, CHUD). Save the
      standings/announcement snapshot before sending and resume after the last
      confirmed message on retry or restart. Test failures at each send and
      define how to handle delivery whose response was lost; exactly-once
      delivery cannot be inferred from a timeout.
- [ ] Track reminder delivery per recipient so a network failure halfway through
      the roster does not repeat successful DMs. Retry transient API errors
      instead of treating every API failure as an unreachable member.
- [ ] Catch up every unposted date with results through the latest due date after
      an outage longer than one day. Test multiple overdue dates and empty days.
- [ ] Reject incomplete or impossible Connections grids before locking a first
      submission: unique solved colours, at most four categories, a terminal win
      or loss, and no guesses after the terminal result. Restrict grid parsing
      to the matched share block and add invalid-grid regressions.
- [ ] Handle SIGTERM cleanly and verify SQLite closure for SIGTERM and SIGINT.
- [ ] Give clear startup errors for a configured webhook, invalid token, or a
      conflicting poller rather than retrying permanent polling errors forever.
- [ ] Respect Telegram's rate-limit retry delay. Decide whether exhausted update
      failures need a durable retry queue; current reply processing tries at most
      three times, then logs and acknowledges the update to keep the queue moving.
- [ ] Define recovery for a stored submission whose confirmation failed: replay
      currently responds "already submitted" rather than restoring confirmation.
- [ ] Verify logs never expose the token or message body, including exceptions.

## P2 — Efficiency and operations

- [ ] Throttle tweet-cache expiry cleanup instead of running two deletes for
      every extracted status ID. Preserve stale-update and expiration semantics.
- [ ] Avoid rewriting roster membership on every ordinary group message; keep
      membership checks and any required last-seen behavior intact.
- [ ] Add Docker log rotation and reasonable CPU/memory limits.
- [ ] Add a health check based on recent successful polling, without launching
      another poller or exposing credentials.
- [ ] Document status, restart-count inspection, deployment, and rollback.

## Remaining automated coverage

- [ ] Test the real HTTP wrapper with mocked responses: success, API/HTTP errors,
      invalid JSON, connection failures, and rate-limit parameters.
- [ ] Test persisted polling offsets and replay across a process restart once
      offset storage is implemented.
- [ ] Test one message containing multiple distinct and repeated tweet links,
      including a failure partway through processing.
- [ ] Test the exact five-day expiration boundary and cache persistence after
      reopening the database.
- [ ] Add shutdown, delivery-resumption, outage catch-up, and invalid-grid tests
      alongside the corresponding backlog fixes above.

## Implemented and verified locally

- [x] Isolate permanent reply failures and malformed updates; later updates and
      scheduled tasks keep running.
- [x] Bound transient update retries, back off failed polling, and isolate reminder
      failures from daily posts. Polling-loop regression tests cover these paths.
- [x] Calculate CHAD/CHUD totals by Telegram user ID. Tests cover identical names,
      duplicate nicknames, name changes, and missing-game penalties.
- [x] Use the latest submitted name for each player within the day's standings;
      configured nicknames still override it.
- [x] Preserve the plural CHUD emoji change and update both expected messages.
- [x] Persist duplicate-link origins and reply claims; prevent duplicate `sf`
      replies after replay and promote deleted origins.
- [x] Collect group-member results by DM, confirm submissions privately, react to
      group submissions, and enforce the first result per game/day.
- [x] Parse Wordle, Krillion, Fermi, and Connections share text; provide `/games`,
      `/leaderboard`, `/chad`, and `/chud`.
- [x] Schedule daily boards and reminders; save posted winner announcements.
- [x] Document configuration, nicknames, local/homelab commands, and the one-bot
      testing workflow; ignore `.env` and local data in Git.
- [x] Provide Make targets and CI for tests, Python compilation, and Docker builds.

## Homelab verification

- [ ] Confirm Docker/Compose installation, secure production `.env` permissions,
      and exactly one running bot container.
- [ ] Confirm Docker starts at boot and `restart: unless-stopped` works after a
      host reboot.
- [ ] Verify rebuilding the container preserves the `sfbot-data` volume.
- [ ] Perform and document one deployment and rollback.
- [ ] Smoke-test original and repeated Twitter/X links, URL variants, expiration,
      chat isolation, deleted origins, and cache survival across restart.
- [ ] Smoke-test member/stranger DMs, first-submission locking, group reactions,
      and all four commands, including tied winners and nickname overrides.
- [ ] Verify 20:00 SGT reminders and 21:00 boards on the live group, including
      post-restart behavior and recovery after a temporary network outage.
