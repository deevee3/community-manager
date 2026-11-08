# Community Manager

Hermes Bot Team #3. Community triage for the Prism channels and inbox:
watches Discord + email, auto-answers FAQs from a local knowledge base, and
turns moderation problems into **flags you decide on in Telegram**.

Self-hosted with Docker on the iMac (or Mac Studio). Long polling, SQLite
state, `.env` config, $0/month.

> **The hard rule:** this bot cannot ban, mute, kick, timeout, or delete
> anything — those calls do not exist in the code (the smoke test greps for
> them). Approve/Dismiss only records *your* decision. You carry out any
> action yourself in Discord/email. Full design: [PRD.md](PRD.md).

## Modules

| Module | File | What it does |
|---|---|---|
| Config | `bot/config.py` | `.env` → typed config; mock mode by default |
| Database | `bot/database.py` | SQLite: intake log, FAQ, flag queue, decision log |
| FAQ | `bot/faq.py` | TF-IDF retrieval over the FAQ store (pure stdlib); seeded from `bot/faq_seed.json` |
| Classifier | `bot/classifier.py` | Rule-based flag taxonomy (spam/scam/abuse/nsfw/off_topic) + optional Ollama re-score |
| Flags | `bot/flags.py` | Flag queue + Approve/Dismiss decision handling (bookkeeping only) |
| Pipeline | `bot/pipeline.py` | Intake → FAQ match → classify → flag |
| Discord intake | `bot/discord_intake.py` | Read-only discord.py listener; mock stream built in |
| Email intake | `bot/email_intake.py` | IMAP poller (mark-seen only); mock stream built in |
| Telegram surface | `bot/telegram_bot.py` | Flag cards with Approve/Dismiss, `/pending /stats /faq_add /digest /id /help` |
| Digest | `bot/digest.py` | Daily pending-flags digest (21:00 ET default) |

## Quick start (on the iMac)

```bash
cd ~/projects/community-manager
cp .env.example .env     # paste your BotFather token + your Telegram user ID
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

It starts in **mock mode**: sample Discord/email traffic flows through the
pipeline and flag cards land in your Telegram DM, so you can tap through the
whole approval flow before connecting real accounts.

## Going live — click by click

**Step 1 — Telegram bot (phone, 5 min).** @BotFather → `/newbot` → name it
(e.g. "Community Manager") → copy the token into `.env` as `BOT_TOKEN`.
`ADMIN_USER_ID` is the same number you used for the Prism bot (ask
@userinfobot if you lost it).

**Step 2 — Discord bot (browser, 10 min).**
1. discord.com/developers → New Application → Bot → Reset Token → copy into
   `.env` as `DISCORD_BOT_TOKEN`.
2. In the Bot tab, enable **Message Content Intent** (so it can read text).
3. OAuth2 → URL Generator → scope `bot` → permissions: **View Channel, Read
   Message History, Send Messages** (send is only for FAQ answers). Do NOT
   tick any moderation permission — that's deliberate.
4. Open the generated URL, add it to your server.
5. Optional: in Discord (Developer Mode on), right-click each channel →
   Copy ID → paste the list into `DISCORD_CHANNEL_IDS`.

**Step 3 — Email (5 min).** For the community inbox: Gmail → create an
**App Password** → fill `EMAIL_IMAP_HOST=imap.gmail.com`, `EMAIL_USER`,
`EMAIL_PASSWORD` in `.env`. The bot only reads UNSEEN mail and marks it
seen; it never sends or deletes email. FAQ-matching mail becomes a suggested
reply card in Telegram — you send it yourself.

**Step 4 — Flip it live.** In `.env` set `MOCK_MODE=0`, then
`docker compose up -d --build` and watch `docker compose logs -f`.

## Configuration (`.env`)

| Variable | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | yes | Telegram bot token from @BotFather |
| `ADMIN_USER_ID` | yes | Your Telegram ID; the only user whose taps count |
| `ADMIN_CHAT_ID` | no | Where cards + digest go (default: your DM) |
| `MOCK_MODE` | no | `1` = sample traffic demo (default), `0` = live |
| `DISCORD_BOT_TOKEN` | live only | Discord bot token (read + send-messages only) |
| `DISCORD_CHANNEL_IDS` | no | Channels to watch; empty = all visible |
| `EMAIL_IMAP_HOST/USER/PASSWORD` | live only | Community inbox IMAP |
| `FAQ_AUTOANSWER_THRESHOLD` | no | Match strictness, default 0.55 |
| `DIGEST_TIME` | no | Daily digest time, default 21:00 ET |
| `OLLAMA_URL` / `OLLAMA_MODEL` | no | Optional local re-scoring on the Mac Studio |

## Day-to-day

- A flag card arrives → read it → **✅ Approve rec.** (records your decision;
  *you* then act in Discord if you want) or **🗑 Dismiss**.
- `/pending` re-sends any cards you missed; the 9 PM digest lists what's
  still waiting.
- `/faq_add Question | Answer` teaches the FAQ — it starts auto-answering
  that question immediately.
- Unanswered email always becomes a `needs_human` flag, so nothing rots in
  the inbox.

## Bot HQ (web interface)

The bot can also surface on the **Bot HQ** dashboard over a small HTTP
bridge (`bot/bridge.py`, stdlib only). It is optional and off until both
`BOT_HQ_URL` and `BOT_HQ_KEY` are set in `.env`:

| Variable | Purpose |
|---|---|
| `BOT_HQ_URL` | Base URL of the Bot HQ dashboard |
| `BOT_HQ_KEY` | This bot's key (sent as the `X-Bot-Key` header) |
| `TRANSPORT` | Where flag cards / approval prompts go: `telegram` (default), `web`, or `both` |

With `TRANSPORT=both`, both surfaces run at once: the same flag appears as
a Telegram card **and** as a dashboard approval (first decision recorded
wins; the other surface then sees it as already decided).

What flows where:

- **Heartbeats** — every 60 s the bot reports `online` to the dashboard
  (with the `chat` capability), whenever the bridge is enabled.
- **Flag cards → approvals** — with `TRANSPORT=web` or `both`, each new
  flag is mirrored to the dashboard as an approval with **Approve /
  Dismiss**. A dashboard decision is recorded exactly like a Telegram tap.
- **History events** — the dashboard history feed gets: flag raised,
  decision recorded (whichever surface decided), and FAQ answered.
- **Dashboard chat** — messages sent to the bot in Bot HQ chat are polled
  every 15 s and answered in the same thread.

> Decisions remain **bookkeeping-only on BOTH surfaces** — the bot never
> enforces, on web or Telegram. Approve only records your decision; you
> carry out any action yourself in the native app.

Dashboard chat commands:

- `/pending` — list flags waiting on your decision
- `/stats` — intake / FAQ / flag counts
- `/digest` — today's digest
- `/approve <id>` — record an approve decision for a flag
- `/dismiss <id>` — record a dismiss decision for a flag
- `/faq_add Question | Answer` — teach the FAQ a new answer

Anything else (including unknown `/commands`) replies with that command
list. With `TRANSPORT=telegram` (default) and the Bot HQ vars empty, the
bot behaves exactly as before — Telegram only, no new requirements.

## Offline check

```bash
python3 -m compileall bot tests
python3 tests/smoke_test.py
```

The smoke test exercises FAQ retrieval, flag creation, the Approve/Dismiss
callback logic, and verifies no enforcement call exists in the source.
