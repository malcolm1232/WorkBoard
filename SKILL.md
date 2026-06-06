---
name: board-steward
description: Tracks active work in a project kanban (board.json + live HTML board served on 127.0.0.1:7891). MUST USE when user says — shipped, deployed, merged, fixed, completed, finished, verified, done, deferred, blocked, paused, moved, add card, log this, track it, what shipped, what's left, status, where are we, what did we do yesterday, sprint, backlog, todo, kanban. Also USE to STAND UP A NEW BOARD when the user says — create a new workboard, new workboard, new board, create a board, set up a board, start tracking this project, bootstrap a board (recipe → docs/BOOTSTRAP.md). Triggers — git commit / push / systemctl restart / scp / rsync that touch prod, or in-progress card whose notes match files just edited. SKIP for pure code questions (debug, explain, refactor, rename) that don't ship anything. Bootstraps on first run by mining ~/.claude/projects/*/*.jsonl for history; streams cards into an empty board with pop/slide animations. Survives sessions: SessionStart hook auto-injects a digest so the board is never forgotten between sprints, branching todos, or week-long contexts.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Grep
  - Glob
---

# Board Steward — LIVE protocol

Keep the project's kanban synced with reality **as work happens**, so the user never has to
ask "did you update the board?" The board is **source of truth, not your memory** — when the
user asks "did we do X?", reach for `card.py` / the digest, not recall.

> **First install / bootstrapping a new board, or wiring hooks & autostart?** → see
> `docs/BOOTSTRAP.md` (the one-time PART 1). This file is the **going-forward LIVE** half.

---

## The LIVE lifecycle — card every unit of work (the spine)

> **The card unit is the work, not the turn.** File when a *unit* starts, gets decided, or ships
> — something you/the user would later reference by `#` or grep in `git log`. One unit spans many
> turns (ask → build → review → ship) under one `#`. **Before opening:** referenced by `#` later?
> → file. Part of an existing card's life? → `subtask` / `fly`, don't open new. Clarifying intent
> before any work? → no card yet. Micro-turn ("yes", "rerun", "open the board")? → no card.

> **Granularity — a top-level card is for USER-named work, not your mechanics.** One card per unit
> the *user* asked for; your internal steps to deliver it (sub-agents you spawn, exploration, a
> deploy/reinstall step, a doc tweak that ships with the change) are **subtasks of that card, or
> nothing** — never their own top-level cards. If the user lists 5 tasks → 5 cards; if one task takes
> 10 internal steps → 1 card + subtasks, *not* 10 cards. Sub-agents follow this automatically via the
> mode dial (`board.settings.subagentCards` / `BOARD_SUBAGENT_CARDS`): **`subtask`** (default) attaches
> a spawned agent's work to the active In-Progress card (none active → nothing); **`collab`** (opt-in)
> gives agent-to-agent product work its own child cards under an epic; **`off`** = silent.

**`fly` is the ONLY column-change verb** — `card.py fly <num> <col>` mutates data AND asserts the
animation contract (~320ms glide + 400ms pause so chained flies don't race the browser). Side-effect
flags: `--note`, `--writeup`/`--writeup-stdin`, `--subtask`, `--bug`, `--improve`. The old `move` was
removed because it jumped. **No "want me to add a card?" prompt — just do it.**

### The three laws (the front of the lifecycle is 100% discipline — nothing enforces it)

1. **Declare, don't record.** The card exists *and* is `fly inprogress` **before the first edit** —
   never `add`+`done` in one breath at session end. A board where every commit has a neat done-card
   can still be fully batched; that post-hoc collapse is the exact miss this kills.
2. **One pulse at a time.** Exactly one card `inprogress` (the coral halo). Many units may wait in
   Task, but only the one you're actively coding is lit.
3. **The Stop hook flags batching — but only after the fact (#74).** On sign-off it now checks each
   card Done this session for in-flight dwell; an `add→done` cluster with no real `inprogress` time
   is surfaced as a "batched-not-live" smell. It does **not block** (the end-state *is* correct,
   just not live-tracked) — it's a mirror, not a gate. Declaring up front (law #1) is still 100% on
   you; the hook only makes the miss visible the next session so it self-corrects.

### Shape → pattern (LAW — every unit MUST match exactly one of these rows)

> **The master discriminator is THE HEADER TEST.** Before choosing a shape, ask:
> *can I write **one honest `verb + noun` header** that covers all the parts in front of me?*
> **Yes** → they're parts of ONE deliverable → **one card, parts as subtasks**.
> **No** → they're independent units → **N cards, one each**.
> The header you can (or can't) write IS the decision — never crammed-into-the-title; the
> parts always live in subtasks, the title stays a clean `verb + noun`.

| Shape | Pattern |
|---|---|
| **1. Single task** | 1 card: `add` → `fly inprogress` (before editing) → `fly done --writeup`. Title = `verb + noun`. The atomic template the others compose from. |
| **2a. Multiple RELATED parts — one honest header** *(passes the header test)* | **1 card + N subtasks, decomposed BEFORE inprogress.** Follow the 5-step order below — do **not** `add`+`fly inprogress` then start working; the subtasks must exist *first*. Title = the shared `verb + noun` header (e.g. `Unify urgent column`), parts **never** in the title. |
| **2b. Multiple INDEPENDENT tasks — no single header** *(fails the header test)* | **N cards.** `add` **all N up front** into Task (so none gets buried — the VISION "task 5 forgotten" case) → fly them `inprogress`→`done` **one at a time**; never light two pulses at once. If you can't name them all with one `verb + noun`, they are NOT one card. |
| **3. Plan mode (multi-step plan)** | 1 **parent** card + `subtask add` per step (a header-test "yes" by construction); fly parent `inprogress`, `subtask done <n> <sid>` at each commit, `fly done` once after final verify — *not* one done-card per step (that shows "done" while the build is half-built). |
| **4. Phase / tier plan** | **1 card PER PHASE**, tagged `phase`, title `Phase N — <goal>`, in **Task**; the phase's deliverables are its **subtasks** (decompose-before-IP applies). The roadmap = N phase cards, glanceable — **never** a wall of one-card-per-deliverable. **Phase cards never go to `inprogress`** (a phase is too big for one pulse). To build a deliverable, **GRADUATE** it into its own card: `add --column task --title "<deliverable>" --link <phase#>` → `fly inprogress`; tick the phase's matching subtask when that card ships. One graduated card in flight at a time. (`card.py fly` **blocks** a `phase`-tagged card from entering inprogress and hands you the graduate command.) |
| **5. Mid-task branch** *(test: does it serve the CURRENT card's goal?)* | "Mid-task" is NOT the test — you're *always* mid-task. The test is **does resolving this serve the current card's goal?** **Yes** (a blocker you must clear to ship this card) → **subtask**, parent **stays `inprogress`**; `subtask add <n> "<finding>" --parent <sid>` the instant it trees out (1→1.1→1.1.1), *before* acting on it; unwind leaf-first, parent `fly done` last. **No** (e.g. doing backend, you spot an unrelated UI bug) → **NEW card** — `add` it into Task, keep your one pulse on the current card, pick it up after. Don't chase the tangent. Use `blocked` only for an external hand-off — it drops the pulse, which is how deep branches get forgotten. |

> ### 🔒 DECOMPOSE BEFORE INPROGRESS — the exact order for shapes 2a / 3 / 4
> A multi-part card's subtasks are created **while it is still in Task**, *before* it ever
> flies to `inprogress`. Decomposition is part of *starting* the card, not part of finishing it.
> **Do this, in this order, every time:**
> 1. `card.py add --column task --title "<clean verb+noun header>" --origin "<their words>"`
> 2. `card.py subtask add <n> "<part 1>"` … `subtask add <n> "<part N>"`  ← **decompose NOW, in Task**
> 3. `card.py fly <n> inprogress`  ← only **after** the subtasks exist
> 4. work each part → `card.py subtask done <n> <sid>` (card shows `1/N → 2/N → …`, struck through)
> 5. `card.py fly <n> done --writeup "…"`  ← once it reads `N/N`
>
> **HARD RULE — no naked multi-part card in IP:** never fly a card that has multiple parts to
> `inprogress` with zero subtasks. If you catch yourself about to, STOP and run step 2 first.
> A multi-part card arriving in IP showing only `1/1` (the auto `☑ initial ship`) is a LAW VIOLATION —
> the parts were lost. (`card.py fly … inprogress` enforces this: it blocks a multi-part-looking
> card with no subtasks unless you pass `--force`.)
>
> **PHASE cards (shape 4) are the exception to step 3:** a `phase`-tagged card **never** goes to
> `inprogress` itself — its deliverables live as subtasks, and you **graduate** the one you're
> building into its own linked card (`add … --link <phase#>` → `fly <new#> inprogress`). The fly
> guard blocks a phase card from entering IP and hands you the graduate command.

### After ship
- **Regression** → `card.py fly <num> inprogress --bug "<what broke>"` — re-flies with the `bug` tag +
  a new open `🐞 fix bug: <reason>` subtask; the next `fly done` closes it, leaving permanent cycle evidence.
- **Enhancement** → `card.py fly <num> inprogress --improve "<what's added>"` — same flow, no bug tag.

**Two layers of truth:** card **column = goal state** (is the high-level goal shipped); **subtasks
= work-cycle history** (one open-then-closed subtask per ship/bug/improve cycle — `☑ initial ship`
on first done, `🐞 fix bug: …` on each reopen). A Done card with open subtasks is a deliberate
"shipped 1/5" — leave it. Cycle subtasks are first-class history forever.

This is the zero-input headline behaviour from `VISION.md` §"The principle". Skip the lifecycle
only for genuine non-tasks (a pure question, debug-this-snippet, explain-X) per the table below.

---

## When to engage (decision table)

The SessionStart hook injects a digest at boot, and the UserPromptSubmit hook re-injects this
protocol every turn (install via `docs/BOOTSTRAP.md` → `--hook all`). This table says when to ACT.

| User said / situation | Action |
|---|---|
| "shipped X" / "deployed Y" / "fixed Z" / "verified" / "done with N" / "landed" | **Must use** — `card.py fly <num> done --writeup "<paragraph>"` |
| "what's left?" / "status?" / "where are we?" / "what shipped today?" | **Must use** — read the digest first; `card.py list`/`query` for slices |
| "add a card for X" / "log this" / "track X" / "save for later" | **Must use** — `card.py add` |
| "move X to backlog/blocked/in-progress" / "this is deferred" / "pause X" | **Must use** — `card.py fly <num> <col>` |
| You start a substantive unit of work | **Must use — card it NOW** (`add` → `fly inprogress`), don't wait for the ship |
| Conversation just shipped something but no card moved | **Must use — backfill NOW.** Don't batch to session end (the #84/#359 drift class). |
| Main Claude just ran `git commit`/`git push`/`systemctl restart` for prod | **Must use** — a real ship; `fly <num> done --writeup "<SHA + what shipped>"` |
| "debug this function" / "why is X failing?" / "explain this code" | **Skip** — board not relevant |
| "rename foo to bar" / pure code edits | **Skip — unless** the work ends in a ship/fix; then fly-card right after |
| User signals public launch (`publish`, `launch`, `go live`, `release`, `make public`, `gh release`, `npm publish`, DNS go-live, repo private→public) | **Must use — gate before action.** Run `card.py prelaunch-check`. Exit 9 = open items; surface them verbatim and ask "OK to launch?" before any irreversible step. |
| User says urgent (`URGENT`, `ASAP`, `P0`, `BLOCKER`, `production down`, `critical bug!`, `it's broken`) | **Must use** — `card.py add` (auto-detects urgency → 🚨 SUPER URGENT col, critical priority). `--urgent` forces, `--no-auto-urgent` opts out. |

**Default bias:** under-engage when uncertain — a missed card is recoverable; an over-eager
skill that interjects on every code question is noise. But once you DO act, act fully: move +
writeup + index regen + bidirectional link if there's a parent.

---

## Reconciliation — keep the board honest

Cards drift: code ships without a `fly done`; commits land that should've been a card; a card
sits `inprogress` after the work shipped. Run a reconciliation pass:

1. **Before answering "what's left / anything else / is everything done"** — run
   `card.py list --column inprogress` + `--column super-urgent`, then grep recent `git log` for
   matching commits. If a card's work is already in HEAD, flag it: *"#N looks shipped at <sha> —
   move to Done?"*
2. **After a commit cluster** with no `fly done` between — scan `git log` since the last ship;
   propose a backfill card for any scope no card covers. (`card.py auto-ship --since-ref HEAD~N`
   scores the match — see `docs/BOOTSTRAP.md` §H.)
3. **Before session end** — every `inprogress` card either ships now (`fly done` + writeup) or
   rolls forward with a `notes` update saying why it's still open.

**Don't silently auto-move cards** — surface drift, let the user confirm. The point is the
*check*, not the *move* (auto-moving re-introduces the silent-drift class).

```bash
python3 ~/.agents/skills/board-steward/scripts/card.py list --column inprogress
git log --oneline --since="$(date -v-2H +%FT%T)"     # commits in the last 2h — anything un-carded?
```

The **Stop hook** is the backstop here: if a turn did substantive work but ran zero `card.py`
calls, it blocks the turn-end and tells you to card it (install via `--hook all`). Don't rely
on it — card as you go — but it's the safety net.

---

## Auto-card on idea-intent (the zero-input promise · #100)

When the user voices a deferred intent that isn't this turn's task, a card materialises on its
own — no "want me to add that?" prompt. The board pops a 5s Undo toast for false positives.

**Fire `card.py add --auto` when the prompt contains a deferred-intent marker** (case-insensitive,
clause-start): `I have an idea[:.]` / `idea[:.]`, `todo[:.]`, `remember to`, `note to self`,
`later we should` / `we should also` / `we'll need to`, `btw can we/could you/should we`,
`what if we` (only if paired with a deferred verb).

```bash
python3 ~/.agents/skills/board-steward/scripts/card.py add \
  --title "<deferred verb-phrase, ≤70 chars>" --auto \
  --auto-source "<the verbatim marker>" --origin "<user's full quoted sentence>"
```

`--auto` defaults `--column` to `💡 Ideas` (creates if missing) and stamps `meta.autoCreated`
(→ Undo toast). **5 hard skips** (guard the over-eager class): (1) they're asking you to do it
*now* this turn — it's the task, not a card; (2) <20 chars after the marker; (3) already an open
card (grep first); (4) recapping/quoting prior convo; (5) `nvm`/`wait`/`actually` in this or the
next 1–2 turns → roll it back. **Under-engage when uncertain** — a missed auto-card is
recoverable; an over-eager one eats trust. Ambiguous marker (`what if we`) → don't fire.

---

## Saving cleanly — always `card.py`

For ~95% of mutations, **don't write Python dict literals** — use `card.py` (handles load +
mutate + `rev` bump + `savedAt`/`savedBy='claude'` + atomic write + `index.json` regen in one
shot). Run from project root (it walks up to find `board/board.json`) or pass `--board <path>`.

```bash
card.py add --code FOO --column task --priority mid --title "..." --origin "..." --link 14
card.py update 32 --priority critical --add-tag urgent
card.py fly 32 done --writeup "Shipped abc1234. Verified on prod (...)."
card.py fly 32 inprogress --bug "drag froze on iPhone"        # reopen w/ 🐞 subtask
card.py subtask add 32 "Eyeball in Safari"   ·   card.py subtask done 32 s-foo-1
card.py link 32 14   ·   card.py unlink 32 14
card.py column add consideration "Consideration" --kind blocked --at 3

# read / filter — progressive-disclosure ladder, cheapest first (pay for what you read):
card.py digest                                    # board pulse, ~120 tok
card.py query --column inprogress --fields num,title    # sliced JSON, only the fields you ask
card.py query --since-days 1 --fields n,code,col        # recently-touched, compact
card.py show 32                                   # one full card (notes, subtasks, writeup, history)
card.py list --column inprogress --priority critical     # human-readable text view
```

**Reach for the lowest rung that answers the question.** "How many open?" → `digest`. "Which
in-progress touched perf?" → `query --column inprogress --tag perf`. "Writeup on #103?" → `show
103`. Reading the full `board.json` to count columns is the anti-pattern this ladder kills.

**Live streaming:** when `serve.py` is up on `127.0.0.1:7891`, every `card.py` call POSTs the new
state → SSE → the browser animates the change. Server down → direct file write, same end state, no
animation. `BOARD_NO_SERVER=1` forces the fallback for batch ops.

For bulk migrations / multi-card transactions / schema surgery only, the raw dict-literal recipe
+ `regen_index.py` is in `docs/BOOTSTRAP.md`. **Never write `index.json` by hand.**

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

The board carries a tag taxonomy at `board.json` → `tagTaxonomy` (max 10 main + 15 sub, each with
a colour). **Read it before tagging** and prefer an existing entry (case-insensitive) — if `bug`
exists, never invent `bugs`/`defect`/`regression-bug`. Use **main** for work type (bug / feature /
infra / security / perf / ui / docs / refactor), **sub** for status/modifier (shipped / deferred /
discuss-first / stress-test / verify / dependency / breaking). Project nouns (`server-g`, `fact9`)
are fine as custom tags (auto-coloured) — but check for a near-synonym first. **Never strip a tag
silently** — ask before retiring one. (Profiles + full rules: `templates/tag-profiles.json`.)

---

## Where the board lives

| File | Role |
|---|---|
| `board/board.json` | **Source of truth.** Read + write via `card.py`. |
| `board/index.json` | Compact digest (one line/card), auto-regenerated on every write. **Read first.** |
| `board/archive/board-YYYY-MM.json` | Done cards >14d (swept at session end). Read only when an archived `#N` is referenced. |
| `board/board.html` | Kanban UI — **don't touch**; it `fetch`es board.json from the server. |

The browser polls `GET /board.json` every 3s and reloads on `rev` change, flashing changed cards
green. v3 works in any browser (one Python process on `127.0.0.1:7891`). **Before touching
`scripts/`, read the architecture tree in `VISION.md`** — new work attaches to the branch that
owns its concern or becomes a new leaf; never rewrite a parent.

---

## What you must NOT do

- **Don't wait until session end to card shipped work.** One update per unit, as it happens — batching is the drift this skill exists to kill.
- **Don't silently auto-move cards at session start.** Report drift; let the user/main-Claude confirm.
- **Don't write to `board.html`** (UI; reads from `board.json` only) or **`index.json` by hand** (regen via the script).
- **Don't reuse `num` values** (always advance `nextNum`) or **break bidirectional links** (add the reverse).
- **Don't summarize away `origin`** (the user's words / historical why) or **fabricate `writeup`** (pull from real SHAs / evidence).
- **Don't read the full `board.json` when `index.json` / `card.py query` would do.** Pay for what you read.
- **Don't write inline dict literals when `card.py` works** — only for bulk migrations / multi-card transactions / schema changes.
