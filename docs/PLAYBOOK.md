# Board Steward — PLAYBOOK (on-demand reference)

Loaded on demand, not part of the always-load `SKILL.md` body. The laws + lifecycle live in
`SKILL.md`; this file holds the fuller reference Claude reads when it needs the detail.

---

## `card.py` — the full recipe sheet

For ~95% of mutations, **don't write Python dict literals** — use `card.py` (handles load + mutate +
`rev` bump + `savedAt`/`savedBy='claude'` + atomic write + `index.json` regen in one shot). Run from
project root (it walks up to find `board/board.json`) or pass `--board <path>`.

```bash
card.py add --code FOO --column task --priority mid --title "..." --origin "..." --link 14
card.py update 32 --priority critical --add-tag urgent
card.py fly 32 done --writeup "Shipped abc1234. Verified on prod (...)."
card.py fly 32 inprogress --bug "drag froze on iPhone"        # reopen w/ 🐞 subtask
card.py fly 32 inprogress --improve "add keyboard shortcut"   # reopen, no bug tag
card.py subtask add 32 "Eyeball in Safari"   ·   card.py subtask done 32 s-foo-1
card.py subtask add 32 "<item>" --parent s-foo-1             # nested subtask
card.py link 32 14   ·   card.py unlink 32 14
card.py column add consideration "Consideration" --kind blocked --at 3
```

### Read / filter — progressive-disclosure ladder, cheapest first (pay for what you read)

```bash
card.py digest                                    # board pulse, ~120 tok
card.py query --column inprogress --fields num,title    # sliced JSON, only the fields you ask
card.py query --since-days 1 --fields n,code,col        # recently-touched, compact
card.py show 32                                   # one full card (notes, subtasks, writeup, history)
card.py list --column inprogress --priority critical     # human-readable text view
```

**Reach for the lowest rung that answers the question.** "How many open?" → `digest`. "Which
in-progress touched perf?" → `query --column inprogress --tag perf`. "Writeup on #103?" → `show 103`.
Reading the full `board.json` to count columns is the anti-pattern this ladder kills.

**Live streaming:** when `serve.py` is up on `127.0.0.1:7891`, every `card.py` call POSTs the new
state → SSE → the browser animates the change. Server down → direct file write, same end state, no
animation. `BOARD_NO_SERVER=1` forces the fallback for batch ops.

For bulk migrations / multi-card transactions / schema surgery only, the raw dict-literal recipe +
`regen_index.py` is in `docs/BOOTSTRAP.md`. **Never write `index.json` by hand.**

---

## Auto-card on idea-intent (the zero-input promise · #100)

When the user voices a deferred intent that isn't this turn's task, a card materialises on its own —
no "want me to add that?" prompt. The board pops a 5s Undo toast for false positives.

**Fire `card.py add --auto` when the prompt contains a deferred-intent marker** (case-insensitive,
clause-start): `I have an idea[:.]` / `idea[:.]`, `todo[:.]`, `remember to`, `note to self`,
`later we should` / `we should also` / `we'll need to`, `btw can we/could you/should we`,
`what if we` (only if paired with a deferred verb).

```bash
python3 <card_py> add \
  --title "<deferred verb-phrase, ≤70 chars>" --auto \
  --auto-source "<the verbatim marker>" --origin "<user's full quoted sentence>"
```

`--auto` defaults `--column` to `💡 Ideas` (creates if missing) and stamps `meta.autoCreated`
(→ Undo toast). **5 hard skips** (guard the over-eager class): (1) they're asking you to do it *now*
this turn — it's the task, not a card; (2) <20 chars after the marker; (3) already an open card (grep
first); (4) recapping/quoting prior convo; (5) `nvm`/`wait`/`actually` in this or the next 1–2 turns →
roll it back. **Under-engage when uncertain** — a missed auto-card is recoverable; an over-eager one
eats trust. Ambiguous marker (`what if we`) → don't fire.

---

## Three text fields — keep them distinct

| Field | When written | What it captures |
|---|---|---|
| **`origin`** | At creation | The WHY. User's words, what triggered it, decision rationale. Don't summarize away. |
| **`notes`** | Ongoing | Current state, in-flight decisions, file paths, commits being tracked. Mutable. |
| **`writeup`** | At Done | The multi-paragraph "how it shipped" — commits, verification, follow-ups. |

A Done card with empty `writeup` is a bug — fill it. Pull writeups from real SHAs / verification
evidence; if you don't have it, ask main Claude. Don't fabricate.

---

## Tag discipline (taxonomy-driven, capped)

The board carries a tag taxonomy at `board.json` → `tagTaxonomy` (max 10 main + 15 sub, each with a
colour). **Read it before tagging** and prefer an existing entry (case-insensitive) — if `bug` exists,
never invent `bugs`/`defect`/`regression-bug`. Use **main** for work type (bug / feature / infra /
security / perf / ui / docs / refactor), **sub** for status/modifier (shipped / deferred / discuss-first
/ stress-test / verify / dependency / breaking). Project nouns (`server-g`, `fact9`) are fine as custom
tags (auto-coloured) — but check for a near-synonym first. **Never strip a tag silently** — ask before
retiring one. (Profiles + full rules: `templates/tag-profiles.json`.)

---

## Phone capture (Telegram) - setup + operation

Text an idea from your phone to your own Telegram bot; it lands in a shared, virtual
**📥 From Telegram** column that renders on every board you run, until someone claims it onto exactly
one of them as a real card.

### One-time setup

```bash
python3 scripts/card.py telegram-setup
```

1. In Telegram, message [@BotFather](https://t.me/BotFather) and send `/newbot`.
Pick a name; BotFather replies with a token that looks like `123456:ABC-DEF...`.
2. Paste the token when the wizard asks (or pass `--token`).
3. Open your new bot in Telegram and send it any message, then press Enter in the wizard.
This is how the wizard learns your `chat_id`, so only messages from you are ever captured.
4. The wizard installs a background job (`launchd` on macOS, `systemd` on Linux) that polls Telegram
every 15 minutes.
On Windows it prints a manual command to run instead, since there is no installed job.

### Removing it

```bash
python3 scripts/card.py telegram-setup --uninstall
```

This removes only the local background job.
It does not touch the saved token, chat id, or your aliases, and it never talks to Telegram - a plain
`telegram-setup` re-run still works afterward without re-pasting the token.
To delete the bot itself, message [@BotFather](https://t.me/BotFather) separately.

### Claiming a capture

- **Drag it.** Open any board; the From Telegram column shows every unclaimed capture across ALL your
boards. Drag one into any real column and it becomes a card there, tagged `from-telegram`, with your
verbatim message as its `origin`. It vanishes from every other board's From Telegram column the moment
it lands.
- **Claim from the CLI**, same effect: `card.py inbox` lists unclaimed captures; `card.py claim <T-n>
[--column <col>]` promotes one onto the board you ran it from.
- **Route it from your phone**, skipping the drag entirely: prefix the message itself with `#<alias>`
(e.g. `#qm add the leverage check`). The bot replies `saved -> qm #431` once the next poll routes it.
Set the alias with `card.py telegram-alias qm ~/path/to/board` (or drop the alias to fall back to a
board's own folder name, which resolves automatically). `--rm` removes a custom alias.
Two boards or two aliases that could mean the same thing never guess - an ambiguous or unknown `#hint`
just leaves the capture in the global column for you to drag by hand.

### What the session digest tells you

The SessionStart hook's board digest surfaces phone captures the same way it surfaces everything else:

- **`CAPTURES: N unclaimed from Telegram, oldest Nd`** - appears whenever there is at least one
unclaimed item, so a capture never silently ages out of view.
- **`⚠ TELEGRAM CAPTURE BROKEN: <reason>`** - appears even with zero unclaimed captures, because a
revoked or rotated bot token is exactly the state where nothing is arriving at all and the digest is
the only place that would ever say so. Re-run `card.py telegram-setup` to reconnect.

### Honest constraints

No server and no hosting cost: the poller is an outbound-only HTTPS `getUpdates` call, so nothing new
ever listens on a port.
Telegram itself drops an unconfirmed message after 24 hours if the machine that runs the poller stays
off that whole time - the bot's `saved` reply on your phone, not the send itself, is the real capture
contract.
A claim reservation that crashes mid-flight self-heals after 120 seconds, so a stuck laptop can never
strand a capture forever.

---

## Reconciliation — the full recipes

The condensed 3-check version is in `SKILL.md`. The supporting commands:

```bash
python3 <card_py> list --column inprogress            # <card_py> = the resolved path from the SessionStart digest
git log --oneline --since="$(date -v-2H +%FT%T)"      # commits in the last 2h — anything un-carded?
python3 <card_py> auto-ship --since-ref HEAD~N        # scores git-log↔card match (see docs/BOOTSTRAP.md §H)
```

**Don't silently auto-move cards** — surface drift, let the user confirm. The point is the *check*,
not the *move* (auto-moving re-introduces the silent-drift class).
