# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

LINE bot ("隊長機器人") that reminds family members to give the dog 隊長 two medicines (犬新寶 every 30 days, 一錠除 every 90 days). Everyone who adds the bot or invites it to a group sees the same shared state. User-facing text is Traditional Chinese.

## Commands

```bash
pip install -r requirements.txt
python -m unittest discover -s tests                       # all tests
python -m unittest discover -s tests -k test_past_date_rejected   # single test
python captain_chat_bot.py                                 # local server (PORT, default 5000); also runs db.create_all()
python scripts/setup_rich_menu.py                          # (re)create the LINE rich menu from assets/rich_menu.jpg; uses .env
```

No linter or formatter is configured. Tests need no `.env` or network: they force in-memory SQLite via `DATABASE_URL` and replace `bot.line_bot_api` with a fake.

## Architecture

Everything lives in `captain_chat_bot.py` (Flask + Flask-SQLAlchemy + line-bot-sdk 2.x). `app.py` is a one-line shim because Vercel only detects fixed entrypoint names.

**Deployment:** Vercel (Hobby), function pinned to Tokyo in `vercel.json` next to the TiDB Cloud Starter database (MySQL-compatible, TLS required → set `MYSQL_SSL=1`). The LINE webhook must answer within 2 s, so keep the cross-region distance and per-event DB round trips small.

**Serverless means no process memory.** Instances are not shared between requests; all state is in the DB. TiDB drops idle connections after ~340 s, hence `pool_recycle=280` and `pool_pre_ping`. There are no migrations: `create_all()` only runs under `__main__`, so new tables or columns must be created by hand against the real DB (run `create_all` locally with the real `.env`).

**State model:** `ReminderState` is a single shared row (`id=1`) holding both medicines' dates and `pending`/`waiting` status. `LineUser.user_id` stores chat targets of all kinds: a user ID for one-to-one chats, a group/room ID for groups (prefix `U`/`C`/`R`). Group members are never registered individually because they may not be friends and cannot be pushed to. `chat_id(source)` picks the right ID. Dates are stored as naive datetimes and interpreted as `TZ_TW` (UTC+8).

**Daily reminder:** Vercel cron (`vercel.json`, `0 22 * * *` UTC, Hobby precision is ±59 min, no retry) calls `GET /trigger_reminder`, which requires `Authorization: Bearer $CRON_SECRET` and rejects everything if the secret is unset. `check_reminder` pushes to every registered chat and flips the medicine to `waiting`.

**Interaction flow (no typing by design):**
- One-to-one: any text, or the rich menu (single full-size area posting `{"action":"show"}`), returns the status card. Groups only answer the keywords in `CARD_KEYWORDS`, and the group welcome message carries a button posting the same `show` action, because rich menus are not shown in groups.
- Dates are picked with `DatetimePickerAction` (`set_date` postback), not typed.
- `done_medicine` / `delay_medicine` / `set_date` reply to the origin chat (free) and `broadcast()` to all other registered chats (push, counted against quota). The `waiting` check makes a second tap a no-op, so it does not broadcast twice.
- Reminder buttons are Quick Replies in one-to-one chats (they vanish on any new message) but a persistent button card in groups, where chatter would otherwise remove them before anyone taps.

## Constraints worth knowing

- The LINE Messaging API cannot unsend, delete or edit sent messages, so stale buttons cannot be hidden.
- Quick Reply and rich menu show only in the mobile apps (not LINE for PC).
- The bot's plan is the free 輕用量 tier: 200 push messages/month, no add-on purchases; reply messages are free. Every broadcast and reminder costs one message per registered chat.
- `assets/rich_menu.jpg` must be 2500x843 and is checked by a test; the font used to render it (內海字體, OFL) is not in the repo.

## Config

Environment variables (`.env` locally, Vercel project settings in production; `.env` is gitignored, never commit it): `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `CRON_SECRET`, `MYSQL_HOST`, `MYSQL_PORT` (TiDB uses 4000), `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB`, `MYSQL_SSL`. `DATABASE_URL`, when set, overrides the MySQL settings entirely and is used by the tests.

## Git

`origin` is the fork `yhzhou0131/captain_chatbot` (SSH alias `github-personal`) and Vercel deploys `main` from it; every push to `main` goes to production. The repo-local git identity is `yhzhou0131`. `upstream` is `easonandclaire/captain_chatbot` over HTTPS and is not writable with that identity.
