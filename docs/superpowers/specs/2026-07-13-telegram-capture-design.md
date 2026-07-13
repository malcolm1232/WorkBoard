# Telegram Idea Capture - Design Spec (card #856)

Date: 2026-07-13
Status: approved design, pending implementation plan
Card: WorkBoard #856

## Problem

Ideas and links appear on the user's phone feed when they are away from the laptop.
They need a zero-friction way to fire those into WorkBoard from the phone, so the next session can keep or act on them.
Today the only path is "remember it until you are back at the laptop", which loses ideas.

## Goals

- Capture from phone in one gesture: send a message or link to a Telegram bot, get a visible confirmation.
- Captured items surface on the boards automatically by the next session, clearly labeled as phone captures.
- No cloud server, no hosting cost, no new always-on infrastructure. $0 forever.
- Product feature: any WorkBoard user can set this up with their own bot in about a minute.
- Per-user privacy: messages flow through the user's own bot and never touch shared infrastructure.

## Non-goals (deferred)

- LLM enrichment of captures at capture time (raw cards only; the user chose "raw card, work later").
- Auto-suggesting claims at session start (revisit after manual claiming has been lived with).
- Remote claim from the phone (replying "-> qm" to route a message later).
- Channels other than Telegram. WhatsApp is explicitly rejected: the official Cloud API is webhook-only (needs a public HTTPS server, a verified Meta Business and a dedicated number), and the unofficial web-puppeting libraries violate ToS and risk banning the user's personal account. Email (IMAP) and Discord both fit the pull model and stay $0, so they remain viable later adapters.
- The architecture keeps this cheap: the channel is confined to the poller. Everything downstream (inbox format, virtual column, claiming, aliases) is channel-agnostic, so a second channel is a new ~50-line poller writing the same inbox lines, with zero changes on the board side. Ship Telegram first; add a channel only when a real user needs one.

## Key decisions

1. **Pull, not push.** Telegram `getUpdates` is a pull API: the bot's messages queue on Telegram's servers (free) and any machine can poll them outbound-only. No webhook, no public endpoint, no open ports.
2. **Poller is a scheduled job, not a server.** A ~50-line stdlib script fired by launchd/systemd every 15 minutes, reusing WorkBoard's existing autostart installers.
3. **One global inbox, virtual column, claim-to-materialize.** Captures live in a single shared inbox file, not in any board.json.
   Every board renders a virtual "From Telegram" column from that file, so the same items appear on ALL boards.
   Dragging an item into a real column of a board atomically materializes it as a real card there and removes it from the global column everywhere.
   This dissolves the "which board does a general capture belong to" problem: no routing decision exists until the user makes one, and general items simply stay in the global column.
4. **Routing decisions happen where context is cheap.** No auto-guessing at capture time. An optional `#<board>` message prefix force-routes when the user already knows; everything else waits for a human (or in-session agent) claim.
5. **24h queue window accepted.** Telegram keeps unconfirmed updates for 24 hours. Mitigations: 15-minute polling while the machine is awake, a catch-up poll at session start, and the bot's "saved" reply as the contract (no reply = not yet captured; the message stays in chat history for manual re-send).

## Architecture

```
Phone: Telegram app -> user's personal bot (created once via @BotFather, free)
                          |  messages queue on Telegram's servers (24h)
Laptop: telegram_poller.py (launchd/systemd, every 15 min, outbound HTTPS only)
        getUpdates -> filter to owner chat_id -> append to inbox.jsonl
        -> bot replies "saved" -> advance offset
Boards: serve.py renders virtual "From Telegram" column from inbox.jsonl
        drag into a real column -> POST /api/inbox/claim -> real card created,
        inbox item marked claimed -> vanishes from all boards (SSE nudge)
Session start: hook injects "N unclaimed captures (oldest Xd)" + catch-up poll
```

## Components

### `scripts/telegram_poller.py` (new)

- Stdlib only (`urllib`, `json`, `fcntl`). No dependencies.
- Config: `~/.board-steward/telegram.json` = `{token, chat_id, offset}`, chmod 600.
- Loop: `getUpdates(offset)` -> ignore messages not from `chat_id` -> append unclaimed items to the inbox -> `sendMessage` a "saved" confirmation -> advance `offset` only after the inbox write has landed.
- At-least-once delivery with dedupe by Telegram `update_id`; re-polling after a crash is safe.
- Parses a leading `#<board-alias>` token into a `routeHint` field; the hint is stored, not executed here.
- Alias resolution is per user and never hardcoded. Aliases derive from the user's own board registry (`~/.board-steward/port-assignments.json`): each board path's project folder name, slugified (e.g. `.../TradingResearch/board` -> `#tradingresearch`). Unambiguous prefixes also match (`#trading`). Users can add custom short aliases in `telegram.json` (e.g. `"qm" -> .../HFTAgents/board`) via setup or a `card.py telegram-alias` subcommand; custom aliases win over derived ones. Ambiguous or unknown alias -> hint ignored, item stays in the global column (never guess).
- The confirmation reply echoes the resolution ("saved -> qm #431" vs plain "saved") so the user knows from the phone whether the hint landed.
- The poller executes a resolved hint by invoking `card.py claim <tid> --board <path>`, the single sanctioned board-write path (it already routes the write through the running server, so the card flies in live). The poller itself never writes board.json. An unresolvable or ambiguous alias is ignored and the item stays in the global column.
- routeHint fallbacks: the hinted board's `task` column is used, falling back to the first task-like column, then to the board's first column.

### `card.py telegram-setup` (new subcommand)

- One-time wizard: paste BotFather token -> "send /start to your bot now" -> captures `chat_id` from the first update -> writes config -> installs the polling job via the existing `install_launchd.py` / `install_systemd.py` / `install_taskscheduler.py` -> runs one poll immediately to prove the pipe end to end.

### `~/.board-steward/inbox.jsonl` + `scripts/_inbox.py` (new)

- Append-only JSONL, one line per capture:
  `{tid, update_id, text, url, ts, routeHint, status, claim}` where `status` is `unclaimed | claimed | discarded` and `claim` is `{board, cardNum, ts}`.
- `_inbox.py` is the single shared library for read/claim/discard, flock-guarded; poller, serve.py, and card.py never touch the file directly.
- Claimed and discarded lines are kept (status flip, not deletion) as an audit trail.

### `serve.py` (extend)

- Renders the virtual column from unclaimed inbox items on every board.
- `POST /api/inbox/claim {tid, column}`: flock -> verify still unclaimed -> create a real card in THIS board via the existing card-create path (tag `from-telegram`, origin = verbatim message, title = message text or URL) -> mark item claimed -> 200 with the new card number. If already claimed: 409 with the winning `board + cardNum`.
- `POST /api/inbox/discard {tid}`: mark discarded.
- SSE nudge on claim/discard so other open boards drop the item live.
- routeHint execution moved to the poller (see below). The server's `GET /inbox` is a pure read with no side effects.

### `board.html` (extend)

- Virtual column with distinct styling and `T-n` badges (not `#n`); items are visually cards but are not board cards yet, so they do not affect numbering, metrics, or recon.
- Drag from virtual column into any real column triggers the claim call; on 409 shows a toast ("already claimed -> qm #431") and removes the item.
- A discard affordance per item.
- Items older than 7 days render dimmed (aging cue against graveyard drift).

### `card.py` (extend)

- `card.py inbox`: list unclaimed captures.
- `card.py claim T-7 --board <path> --column notes`: CLI parity so the in-session agent can claim.

### Session-start hook (extend)

- Digest line: "N unclaimed captures (oldest Xd)" when the inbox is non-empty.
- Fires a catch-up poll (same poller, one shot) so machines without the background job still pull the last 24h.
- Surfaces poller health: "Telegram poller: token invalid" style warnings instead of silent failure.

## Error handling

- Network down or Telegram unreachable: poller exits silently without advancing offset; next tick retries; dedupe makes retries safe.
- Invalid or revoked token: poller writes a status marker file; the session-start digest surfaces it.
- Messages from strangers (anyone can find a bot): ignored via the `chat_id` allowlist; the board is not publicly feedable.
- Claim race between two open boards: flock, first claim wins, second receives 409 and shows the toast.
- Corrupt inbox line: skipped with a warning; `doctor.py` gains an inbox integrity check.
- Token security: chmod 600, never logged, never leaves the machine.

## Testing

- Unit: claim atomicity under concurrent claimers; `update_id` dedupe; `#prefix` parsing; offset-advance ordering (offset must move only after the inbox write).
- Integration: a local HTTP stub stands in for the Telegram API; run the poller against it and assert inbox contents, confirmation reply, and offset; hit the claim endpoint and assert the real card (tag, origin, title) plus the claimed status flip.
- E2E via the existing `/e2e` throwaway-board harness: seed an isolated inbox with 3 items, open two throwaway boards, drag-claim on one, assert the item vanishes from the other, a real card exists with correct tag/origin, and the live board is untouched.
- Ship gate: manual end-to-end with the user's real phone and bot -> message -> "saved" reply -> card on the real board.

## Setup UX (per user)

1. Message @BotFather: `/newbot`, pick a name -> receive token (about a minute, free).
2. Run `card.py telegram-setup`, paste the token.
3. Send `/start` (or anything) to the new bot when prompted; setup captures the chat id and installs the background job.
4. Send a test link; see the "saved" reply; see it appear in the From Telegram column.
