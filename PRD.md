# Community Manager — PRD (Hermes Bot Team, Bot #3)

**Status:** v1 built 2026-10-20. **Cost:** $0/month (hard constraint).
**One line:** day-to-day community triage for D'Vaughn's groups — it reads,
answers FAQs, and *flags* problems. It never punishes anyone. Every
enforcement decision is his, made with a tap in Telegram.

---

## 1. What it does

| Job | How |
|---|---|
| Watches his communities | Discord listener (read-only) + email inbox poller (IMAP) |
| Answers routine questions | FAQ retrieval over a local knowledge base; high-confidence matches get an instant answer, logged |
| Spots trouble | Rule-based classifier (+ optional Ollama pass on the Mac Studio) sorts messages into a flag taxonomy |
| Surfaces decisions | Each flag becomes a Telegram card with the message, the reason, and a *recommended* action — Approve / Dismiss buttons |
| Reports | Daily digest of pending flags + what got auto-answered |

**Out of scope (by design):** Skool (no public API — unofficial scraping is
fragile, per blueprints v0.1 verification). Telegram group moderation itself
stays with the Prism bot; this bot's Telegram side is only the decision
surface for Discord + email.

## 2. The hard rule — enforced in code, not just docs

**The bot can never ban, mute, kick, timeout, or delete anything, anywhere.**

- The codebase contains **zero enforcement calls**. There is no
  `ban_chat_member`, `delete_message`, `restrict_chat_member`, Discord
  `member.ban/timeout/kick`, or message-delete call in any module. The smoke
  test greps the source for these names and fails if one appears.
- The Discord bot is invited with read + history + send-messages
  permissions only (send exists solely to post FAQ answers). It never
  requests any moderation permission (no manage-messages, kick, ban, or
  timeout), so Discord itself refuses enforcement even if asked.
- "Approve" on a Telegram card records **his decision** (flag id, action he
  approved, timestamp) in SQLite. It does not execute the action. He carries
  it out in the native app (or not — dismissing is one tap).
- FAQ auto-answers are the only autonomous output, and they are *answers*,
  not enforcement. Every auto-answer is logged and appears in the digest.

## 3. Intake channels

| Channel | Module | Mode |
|---|---|---|
| Discord | `bot/discord_intake.py` — discord.py listener on configured channel IDs; read-only | Live when `DISCORD_BOT_TOKEN` set; otherwise mock |
| Email | `bot/email_intake.py` — stdlib IMAP poller on an interval | Live when IMAP creds set; otherwise mock |
| Mock | Both intakes emit realistic sample traffic when `MOCK_MODE=1` (default for first run) | Always available |

Every intake item lands in SQLite (`intake` table) with source, author,
channel, text, and timestamp, then flows through the same pipeline.

## 4. FAQ retrieval design

**Choice: TF-IDF cosine retrieval in pure stdlib** (no FAISS, no Chroma).

Justification: the FAQ base for one person's communities is small (tens to a
few hundred Q&A pairs). At that scale TF-IDF cosine similarity is exact,
deterministic, dependency-free, and runs anywhere — FAISS/Chroma would add
heavy native/service dependencies for zero quality gain. If the base ever
grows past ~5k entries, swapping the retriever is a one-module change
(`bot/faq.py` isolates it).

- Store: `faq` table in SQLite, seeded from `bot/faq_seed.json` on first run;
  editable live via `/faq_add` in Telegram.
- Match: question text tokenized → TF-IDF vectors → cosine vs. FAQ questions.
- Threshold (`FAQ_AUTOANSWER_THRESHOLD`, default 0.55): at/above → auto-answer
  (Discord reply / email reply draft surfaced in Telegram for email, since
  auto-sending email is out of scope); below → treated as an unanswered
  question and counted in the digest so he can add it to the FAQ.

## 5. Flag taxonomy

| Category | Meaning | Default recommendation |
|---|---|---|
| `spam` | Repetitive promos, link dumps, bot-like bursts | Recommend: remove + warn (manual) |
| `scam` | Fraud signals: fake giveaways, impersonation, seed-phrase bait | Recommend: remove + ban (manual) |
| `abuse` | Harassment, slurs, targeted attacks | Recommend: warn / timeout (manual) |
| `nsfw` | Explicit content in SFW spaces | Recommend: remove (manual) |
| `off_topic` | Persistent derailing of a focused channel | Recommend: redirect, no action |
| `needs_human` | Anything the classifier can't sort confidently, plus all email that isn't a clear FAQ | Recommend: read it yourself |

Recommendations are labels on the card. Nothing executes them.

## 6. Approval flow (Telegram)

1. Flag created → bot sends a card to `ADMIN_CHAT_ID`: source, author,
   excerpt, category, confidence, recommendation, full-text button payload.
2. Buttons: **✅ Approve rec.** / **🗑 Dismiss**. Callback data:
   `flag:<id>:approve` / `flag:<id>:dismiss`.
3. Tap → decision written to `decisions` table (flag id, decision, decider,
   timestamp) → card edited to show the outcome. Only `ADMIN_USER_ID`'s taps
   count; anyone else's are ignored.
4. Pending flags (no tap yet) roll into the daily digest at `DIGEST_TIME`.

Admin commands: `/pending` (list open flags), `/faq_add Q | A`,
`/stats` (intake/flag/answer counts), `/help`, `/id`.

## 7. Stack & deploy target

- Python 3.12, python-telegram-bot v21, discord.py, APScheduler,
  python-dotenv — same conventions as the Prism bot package.
- SQLite state in `./data/` (Docker volume), config only via `.env`.
- Optional Ollama (`OLLAMA_URL`, Mac Studio) re-scores borderline flags;
  if unreachable, rules-only classification continues. Never a hard dep.
- **Deploy target:** Docker on the iMac (same as Prism bot) or the Mac
  Studio — whichever is always-on. One container, long polling, no open
  ports. See README for click-by-click steps.

## 8. His steps to go live

1. @BotFather → new bot token → `.env` (same drill as Prism).
2. Discord developer portal → bot user with **read-only** permissions →
   token + channel IDs → `.env`. (He creates it; nobody else can.)
3. Email: app password for the community inbox → `.env`.
4. `docker compose up -d --build`, watch logs, tap a test flag.
