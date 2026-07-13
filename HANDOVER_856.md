# Handover: #856 Telegram phone capture (session of 2026-07-13)

## TL;DR for the next session

The feature is **built, reviewed, and green, but NOT merged and NOT shipped**.
Everything lives on the branch `feat/telegram-capture-856` (26 commits off `14882d8`).
The one remaining step needs a human with a phone, so an agent cannot do it.

**Do this first:** ask the user whether they have run the real-phone gate (below).
Do NOT re-run the implementation. Do NOT re-dispatch the plan.
The SDD ledger at `.superpowers/sdd/progress.md` marks all 10 tasks COMPLETE; trust it and `git log` over any instinct to redo work.

## What it does

The user texts a link or an idea to their OWN Telegram bot from their phone.
A local poller (launchd, every 15 minutes, outbound HTTPS only) pulls it into ONE shared inbox at `~/.board-steward/inbox.jsonl`.
Every board renders a VIRTUAL "From Telegram" column from that shared file.
Dragging an item into a real column claims it: a card is created on THAT board and the item vanishes from every other open board over SSE.
A `#<board>` message prefix routes a capture straight to one board.

No server, no hosting, no cost. Nothing new listens on a port.

## The ONLY thing left: the real-phone ship gate

The user runs this. It is deliberately not automated.

1. In Telegram, message `@BotFather` -> `/newbot` -> copy the token.
2. `cd ~/Desktop/WorkBoard && python3 scripts/card.py telegram-setup`, paste the token, then message the new bot once when prompted.
3. Send the bot a link. Expect a `saved` reply on the phone, and the item appearing in the "From Telegram" column on every board.
4. Drag it into a column. Expect a real card, tagged `from-telegram`, with the verbatim message as its origin.
5. Send `#workboard some idea`. Expect the reply `saved -> workboard #<num>` and the card landing on that board directly.
6. Confirm a claimed item disappears from the OTHER board's column.

If all six pass: `scripts/card.py fly 856 done --writeup "..."` and merge `feat/telegram-capture-856`.
If something misbehaves, capture what the user actually saw before touching code.

## Board state

- **#856** - In Progress. The parent card. All 10 subtasks ticked. Notes on the card point at the spec and the plan.
- **#868** - a genuine PRE-EXISTING bug found in passing: the first `card.py add` on a brand-new board crashed (`activeWork: null` was not normalized). Fixed on this branch.
- **#869** - Accepted Tradeoff: there is no two-phase commit across `inbox.jsonl` and `board.json`. Read this before "fixing" the claim path.
- **#873** - Idea: a shared `@WorkBoard` bot (one bot, many subscribers) instead of a per-user BotFather bot. Verdict recorded: needs a hosted server, so it is parked. The full reasoning is on the card. Do not re-derive it.
- **#874** - Task: cut `telegram-setup` friction (BotFather deep link + clipboard token auto-detect) while KEEPING the per-user bot. This is the answer to "BotFather is annoying", not #873.

## Where things are

- Spec: `docs/superpowers/specs/2026-07-13-telegram-capture-design.md`
- Plan: `docs/superpowers/plans/2026-07-13-telegram-capture.md`
- SDD ledger (per-task commits, review outcomes, gotchas): `.superpowers/sdd/progress.md`
- Docs shipped for users: a "Capture ideas from your phone" section in `README.md`, one line in `SKILL.md`, a fuller walkthrough in `docs/PLAYBOOK.md`.

## Code map

| File | Role |
| --- | --- |
| `scripts/_inbox.py` | The ONLY module that touches `inbox.jsonl`. Claim state machine with reservation tokens. |
| `scripts/_tg_config.py` | Bot credentials + per-user alias resolution (derived from the user's own board registry, never hardcoded). |
| `scripts/telegram_poller.py` | The only file that knows Telegram exists. `getUpdates` -> inbox -> confirm. |
| `scripts/card_commands.py` | `cmd_claim`, `cmd_inbox`, `cmd_telegram_setup`, `cmd_telegram_alias`. |
| `scripts/serve.py` | `GET /inbox`, `POST /inbox/claim|discard|notify`. |
| `templates/board.html` | The virtual column, drag-to-claim, SSE. |
| `scripts/card_state.py` | `build_card` (shared card constructor) + a hardened `atomic_save`. |

Tests: 12 suites, `dev/test_856_*.py`, all green. Run them with
`for t in dev/test_856_*.py; do python3 "$t" | tail -1; done`.

## Invariants a future agent MUST NOT break

1. **The virtual column must never enter `state.cards` or `state.columns` in `board.html`.**
   The browser POSTs its ENTIRE `state` object on save, so a leak writes the virtual column permanently into the user's `board.json`.
2. **Both claim paths must stay identical.**
   `cmd_claim` (CLI, also used by the poller for `#alias` routing) and `_handle_inbox_claim` (HTTP, used by the browser drag) must behave the same: reserve first, same-board dedupe on `meta.telegram.tid`, re-verify token ownership immediately before the write, release ONLY before the write succeeded, never crash if `finalize` fails.
3. **Any claim or discard must call `_inbox.notify_boards()`**, or other open boards keep showing an item that is already claimed. This was the bug the final review caught; `dev/test_856_cross_board_sse.py` guards it with two real servers and a real SSE subscription.
4. **`atomic_write` already takes the board lock.** Do not wrap it in `_boardio.board_lock` again: nesting the same-process flock stalls every claim for 5 seconds.
5. **The poller must never write `board.json` itself.** Its only board-write path is invoking `card.py claim`.
6. **`GET /inbox` is a pure read.** An earlier design executed route hints as a side effect of a render; that was deliberately moved to the poller.
7. **`card.py claim` must keep printing a line starting `+ #<num> `.** The poller anchors `^\+\s*#(\d+)` to it to echo the card number back to the phone.
8. **`hook_session_start.sh` runs at the start of every session.** It must always exit 0 and never block.

## Lessons from the reviews (why the code looks the way it does)

The final whole-branch review caught what nine per-task reviews missed: **the browser drag-claim never notified the other boards**, so claiming on board A left the item sitting on board B indefinitely - the feature's whole promise.
It survived that long because the end-to-end test asserted board B's state by re-reading the shared file rather than by listening to the live push, so it passed while the bug was present.
The lesson: a test that re-reads shared state does not prove a notification was delivered. Test the mechanism, not the side effect.

Two other defects worth remembering, both caught only because a reviewer was hunting for them:
- A revoked bot token used to fail **silently forever**: captures just stop, with no signal. It now surfaces as a `TELEGRAM CAPTURE BROKEN` warning in the session digest.
- A stalled claim could produce **two cards for one idea** via the stale-reservation self-heal. Closed by an ownership re-check plus a dedupe guard; the residual is #869.

## Known gaps (deliberate, not forgotten)

- Windows gets a printed manual scheduling command instead of an installed job.
- Telegram drops unconfirmed messages after 24 hours, so a machine that never comes online for a day loses a capture. The bot's `saved` reply is the capture contract: no reply means it did not land.
- A corrupt inbox line is skipped and warns loudly on stderr, but is still dropped on the next rewrite. Preserving it verbatim would need a larger change to the write path.
- `doctor.py` has no inbox integrity check yet.
