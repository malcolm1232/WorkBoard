# VISION — `board-steward`

## Goal

Be **the #1 kanban board for Claude and the humans Claude works with.**

Not "a kanban that supports Claude." A kanban built _from the ground up_ around the reality that the agent and the human ship code together — fast — and the board is the only thing keeping both honest about what's done, what's in flight, and what got forgotten in a branch three levels deep.

## The problem this exists to solve

Work with Claude moves so fast that **tasks branch faster than humans can track them.**

A typical session:

```
Task 1.    Fix the auth bug.
   └─ 1.1  Migration breaks on Postgres 16
       └─ 1.1.1  Locking behavior changed in PG16
       └─ 1.1.2  Need to backfill the new column under load
   └─ 1.2  Session token format drift between staging/prod
Task 2.    Add the rate-limit header.
Task 3.    …
Task 4.    …
Task 5.    [forgotten — buried 3 levels deep in Task 1]
```

By the time Claude resolves `1.1.2`, **item 5 is gone.** The user re-asks for it three days later thinking it was missed; Claude apologizes and re-does discovery from scratch. Multiply by 30 sessions a week.

This is the failure mode every other kanban (Trello, Linear, Notion, Asana) is **structurally unable to solve**, because they assume a human types every card. The human typing is the bottleneck. By the time you've finished typing "fix locking behavior changed in PG16," you've forgotten task 5.

## The principle

> **Zero input from the user. Work auto-logs.**

- User says "I have an idea: …"  →  card appears in **Ideas**, animated in, in real time
- Claude starts implementing it    →  card slides to **In Progress**
- Claude hits a blocker            →  card slides to **Blocked** with the reason as the note
- Claude ships it                  →  card slides to **Done** with a multi-paragraph write-up of _how_ it was done (commit SHAs, files touched, verification)
- Claude branches mid-task         →  subtasks tree out _inside_ the card; the parent never leaves the screen

The user **glances at the board** — does not type, does not drag, does not configure — and knows the full state of the work. The board is a **living dashboard of the collaboration**, not a project-management tool that the human has to keep fed.

## What "the #1 kanban" means concretely

It wins on four axes — each axis the others ignore:

### 1. UI/UX maxed out

- **Live motion.** Cards pop in with overshoot easing (`cubic-bezier(0.34, 1.56, 0.64, 1)`) the instant they're created. Cards moving between columns animate with the FLIP technique — they _glide_ from one column to another, not teleport. Columns slide in. The user _sees_ the work flowing in real time, like a trading dashboard, not a database.
- **Glanceable, not clicky.** Origin (the "why this card exists") shown as a hover tooltip on the home view — no click required to understand any card. Time-since-update inline on the head row. Priority is a color stripe. Code badges (e.g. `FACT-F2`) for shared vocabulary.
- **The calendar tab is a real calendar** — not a list view in a grid skin. Each card lands on its done-day or created-day; the user can see "we shipped 17 things on May 25" at a glance.
- **No setup screens.** No "click here to open file." Open the URL, the board is there.

### 2. Token-efficient by design

Most agent tools fail here. They dump 100KB of state into the context window on every turn and call it "memory."

`board-steward` is the opposite:

- **Tiered reads.** SessionStart hook injects a **~150-token digest** (`🚨 SUPER URGENT: 3 · 📌 MANDATORY: 5 · Backlog: 37 · Done: 44 · Last shipped: #98 (<1h ago)`). The full board.json is _never_ loaded unless a specific card is referenced.
- **CLI primitives.** Claude queries the board via `card.py show <num>` or `card.py list --status doing` — subprocess returns sliced JSON, the agent never reads the 113KB file.
- **Progressive disclosure ladder.** Digest at boot → one card on demand → full thread only when truly necessary.
- **Index.json digest** (~34KB) regenerated atomically after every write, for the rare case the agent needs a sweeping view.

Target: **<2KB of context** consumed by the board on a typical session start, vs ~147KB for a naive design. Measured, not aspirational.

### 3. Startup is instant and invisible

- **Autostart.** macOS `launchd` plist (Linux: systemd unit, Windows: scheduled task) brings the local server up at boot. The user never starts anything. The URL `http://127.0.0.1:7891` just works.
- **Hook fallback.** If `launchd` ever dies, the `SessionStart` hook detects the dead port and re-spawns the server in the same turn. The user never sees a broken board.
- **Auto-bootstrap.** On first install, `discover.py` mines `~/.claude/projects/*/*.jsonl` to seed the board with cards inferred from prior chat history — so the user opens the board for the first time and **already sees their last week of work**, chronologically, animated in card-by-card. No empty-state to fill.
- **One command to install** in any project: `python serve.py --bootstrap`. No config file. No account. No cloud.

### 4. Minimal friction — Claude does everything

The hardest design constraint, and the one that separates this from every kanban that came before it:

- **Auto-card-creation.** When the user says "I have an idea: X," Claude detects intent and runs `card.py add` — a card pops up in Ideas in real time, animated in over 320ms. No "want me to add this?" prompt. No user typing.
- **Auto-promotion.** When Claude finishes the task, it runs `card.py fly <num> done` with a writeup. Card glides to Done. Hooks fire on the user's next prompt to confirm — no out-of-band "are we good?" check needed.
- **Auto-discovery.** When Claude does work matching an open card's notes (e.g. edits a file referenced by an in-progress card), the hook surfaces the linkage so the card gets updated atomically with the code change.
- **Self-gating.** Dense-keyword skill description so Claude knows when to engage (`shipped`, `deployed`, `merged`, `done`, `blocked`) and when to skip (`debug this`, `explain X`, `refactor Y` — pure code questions that don't ship). No drift in either direction.
- **Self-telemetry.** Every Steward invocation logs a JSONL event (frequency, issues, read efficiency, bookend compliance) so the skill _measurably improves itself_ over time — the user runs `report.py --days 14` and sees what's broken.

## Non-goals

- **Not a multi-_user_ team tool.** No accounts, no permissions, no real-time collab between humans. This is the agent-and-one-human board. Teams use Linear. (Multi-_board_ — one board per project for the SAME human — IS supported; see "Multiple boards" below. "Multi-board" ≠ "multi-user".)
- **Not a cloud product.** Everything is local files + localhost HTTP. The user owns their data; no signup; no telemetry leaves the machine.
- **Not a project-management tool.** No Gantt charts, no resource allocation, no sprint planning. The board reflects the work, it doesn't direct it.

## Distribution

- Installed as a Claude Code **plugin** (`board-steward@skills-dir`, symlinked into `~/.claude/skills/board-steward`): it bundles the board-steward *skill* — so it shows up in the available-skills list and triggers on the right user phrases automatically — plus the four `SessionStart`/`UserPromptSubmit`/`PreToolUse`/`Stop` hooks (in `hooks/hooks.json`, loaded via `${CLAUDE_PLUGIN_ROOT}`), so no hand-wiring in `settings.json`.
- Source-of-truth repo at `~/Desktop/WorkBoard/`, published as open source.
- `python serve.py --bootstrap` is the one command anyone runs to get going. No npm, no Docker, no Postgres — pure stdlib Python + a single HTML file.

## The test

A user installs `board-steward`, opens Claude in their project, and says: _"build me a login form."_

Within 5 seconds, three things happen with **zero user input**:

1. The kanban opens at `http://127.0.0.1:7891` in their default browser.
2. A card pops into **In Progress** titled "Login form" with the user's exact phrasing as the origin.
3. As Claude works, sub-cards branch out (route, form component, validation, tests), each animating in. When tests pass, the card glides to **Done** with the commit SHA and a one-paragraph writeup.

The user did not type a single thing into the kanban. They glanced at it once and knew the full state of the work.

**That is the bar.** Anything less is just another kanban.

---

## Multiple boards (one per project)

The original design assumed **one board per machine**. It now supports **many boards at once — one per project** — for the same single human. This is not multi-user; it's one person who works across several repos and wants each to have its own living board.

The workflow has **two parts**:

### Step 1 — REPEAT HISTORY (one-time bootstrap per board)

When a project gets its first board, `serve.py --bootstrap` (via `bootstrap_project.sh` or the first-run picker) mines that project's prior Claude session history and seeds the board with cards inferred from it — so the board opens already showing the project's recent work, animated in. Each project bootstraps independently; bootstrapping a second project never touches the first.

### Step 2 — LIVE (going forward, multi-board)

Every board is **isolated and self-routing**:

- **Sticky per-project ports.** `port_registry.assign(board_dir)` gives each project a deterministic port (7891 upward) that survives restarts. Two projects can never collide. The server has **no hardcoded port default** — the registry assigns it.
- **Routing by location.** `card.py add/fly/show` and the hooks resolve *which* board to talk to by walking up from the cwd / edited file for `board/board.json`, then looking up that board's port in the registry (probe only as a fallback). A card filed from inside project A lands on A's board, never B's.
- **`$HOME` disambiguation = last-active, not mtime.** When Claude opens in `$HOME` (no project in cwd) and several boards exist, the SessionStart hook reopens the **last-active board** — the one whose cards the human last mutated, or the one they just bootstrapped — via `port_registry.get_active()` / `set_active()` (`~/.board-steward/last-active`). It does *not* guess by newest `board.json` file mtime (that picked the wrong board when two boards were touched the same session). Falls back to mtime only when no active pointer exists yet.

`port_registry.py` is the single owner of all cross-board state in `~/.board-steward/`: the liveness registry, the sticky port-assignment map, and the last-active pointer.

# Pending: no UI for switching between boards from within one board's HTML view (today you open each board's own URL/port). # Pending: no "list all my boards" command surfacing every registered board + its port in one place.

---

## Reconciliation (keeping the board factually correct)

The board is only useful if it's a **faithful mirror of reality**. Reconciliation is the process that keeps it honest — it's bidirectional truth-making, four moves:

1. **In-Progress → Done** when the activity shows the work actually shipped (the "*the card sat In-Progress for ages but it was all done*" case — the **most common drift**).
2. **Surface MANDATORY/IMPORTANT** — anything flagged urgent/critical/must/p0/blocker that never got prioritized → the `mandatory` column. This is the **star**: never silently lose an important point.
3. **Capture un-carded work** — substantive edits/commits that never became a card at all.
4. **Demote abandoned** — "skip/nvm/defer/later" → `backlog`; stale-untouched (>24h, no follow-up) → `backlog`. The board shouldn't imply work is live when it isn't.

### Two complementary layers

- **Live carding backstop — the Stop hook** (`_hook_stop_recon.py`). Cheap, **no LLM**. On sign-off it checks: did this turn ship work (edits/commits) but run no `card.py`? If so it *blocks* and says "card it now." This is the in-the-moment guarantee for **move 3** (net-new un-carded work). Always on, ~free.
- **Smart reconciliation — Haiku** (`hourly_reconcile.reconcile_sweep`). A `claude -p --model haiku` pass that reads the recent activity and applies **moves 1, 2, 4** to existing cards, animating them on the live board. Runs at **two triggers**:
  - **Bootstrap** — after the initial history-mine fills the board (post-extraction sweep).
  - **SessionStart** — `hourly_extractor --reconcile-only`, spawned **detached** so the digest stays instant. Reconciles *every* non-done card (`only_discovered=False`) against the activity since the last recon.

### Why these triggers (not others)

- **End-of-session is unreliable** — users just close the terminal, so a Stop-time smart pass would often never run. SessionStart catches the *previous* session's drift before the user starts the next one.
- **Mid-session is too costly** — re-running a Haiku pass every few turns burns tokens for no clear gain.
- So the smart pass lives at **SessionStart + Bootstrap**, and the cheap blocking backstop lives at **Stop**.

### Why Haiku, and the cost gates

The smart pass uses **Haiku**, not the in-session (Sonnet) main agent: Haiku is far cheaper, which matters because **free users have a minimal token budget** (Max users have ~1M). It uses the user's existing Claude login — no API key. SessionStart recon is **gated** so it never burns a call needlessly: it fires only when (a) there are non-done cards AND (b) there's new project activity since the last recon (tracked in `board/.recon_state.json`). A quick re-open with nothing new costs nothing.

`# Pending:` SessionStart recon is reconcile-*only* (moves existing cards). Catching net-new un-carded *important* work at SessionStart (an extract pass over the gap) is deferred — the Stop backstop covers net-new live for now.

---

## Operating rules (Claude reads this every session)

These are durable, project-level instructions. Claude follows them on every turn while working in this repo or anything under `~/Desktop/`.

### 🚨 RULE 0 — MANDATORY SESSION STARTUP (do this BEFORE anything else)

**On the very first turn of every session, before responding to the user's request, before any tool call other than Read, before acknowledging anything — read BOTH files:**

```
~/Desktop/conversation_history/conversation_raw_YYMMDD.md   ← TODAY  (local date)
~/Desktop/conversation_history/conversation_raw_YYMMDD.md   ← YESTERDAY (local date − 1)
```

Where `YYMMDD` is the local date (e.g. `260527` = 2026-05-27, `260526` = yesterday).

Rules for this step:

- **Not optional.** Do it even if the user's first message looks trivial ("hi", "thanks", "ok"). Skipping is the #1 cause of "we already discussed this" drift.
- **Not announced.** Do not ask "want me to read the history first?" — just read.
- **Not summarised away.** Read the raw files. Do not rely on MEMORY.md / digests / carry-forward bullets as a substitute — those are lossy.
- **Both files.** If today's file doesn't exist yet (first session of the day), read yesterday's + the day before. Never read just one.
- **Then proceed.** Acknowledge what you saw in one line ("Read. Today = X sessions, last shipped Y. Yesterday = Z."), then answer the user's actual request.

If you find yourself responding to a substantive prompt without having read these two files this session — **stop, read them, then continue.**

### Other rules

1. **Every code change must leave the codebase ARCHITECTURALLY CLEAN.** Correctness (tests pass) is necessary but **not sufficient** — a change that works but degrades the structure is not done. Before and after any non-trivial code change, check it against the definition below; if the change introduces a new architectural smell, fix it as part of the same work or stop and flag it.

   **"Architecturally clean" means (the checklist):**
   - **Clean modules.** Each file has ONE clear job and a name that says it. No grab-bag modules. New behavior goes in the module that owns that concern, or a new focused module — never bolted onto an unrelated one.
   - **Bounded files.** Files stay readable (rough ceiling ~1,000 LOC). When a file outgrows its job, split it along concern lines (the #307 split pattern), keeping the public interface stable.
   - **No god-functions.** A function does one thing. Long-but-FLAT (a declarative list, a linear sequence) is acceptable; long-AND-TANGLED (deep nesting + mixed concerns) is not — extract named helpers. **Function length is a style hint, not the test; nesting depth + mixed concerns is the real smell.**
   - **Shallow, one-directional coupling.** Leaf utilities (`_boardio`, `_render`, `_metrics`, `_hook_*`) are imported by callers; they never reach back up. **Zero circular imports** — verify with a clean import of every module.
   - **No duplication.** One source of truth. The repo `scripts/` is canonical; the installed plugin dir is synced from it by hook (#302) — never hand-edit the copy.
   - **Don't branch forever.** Architecture = boundaries, coupling, duplication, dependency direction. Once those are clean, STOP — do not chase every function under an arbitrary line count. That loop never converges and is not what "clean" means.

   **Current architecture (the tree — keep this accurate when modules change):**

   ```
   WorkBoard/                          repo root (canonical source)
   ├── install.sh                      one-command installer (--demo / --harvest / --fill)
   ├── SKILL.md                        PART 2 — the lean going-forward LIVE protocol (per-turn card→IP→done); loaded every session
   ├── VISION.md  README.md  CHANGELOG.md
   ├── templates/                      shipped assets (copied into a new board)
   │   ├── board.html                  the live kanban UI (FLIP motion, SSE client, HUD)
   │   ├── board.json                  empty-board seed
   │   └── tag-profiles.json           per-profile tag taxonomies
   ├── scripts/                        ★ the engine — every module = ONE job
   │   │
   │   ├── card.*                      ── BRANCH: board CLI + state ──
   │   │   ├── card.py                 entry / arg-dispatch / server-vs-direct write
   │   │   ├── card_state.py           load · save · flock · schema · index regen
   │   │   └── card_commands.py        the cmd_* handlers (add/move/fly/subtask/…)
   │   │
   │   ├── serve.*                     ── BRANCH: local HTTP + SSE server ──
   │   │   ├── serve.py                runtime: handler · routes · SSE · _run_server
   │   │   └── serve_bootstrap.py      bootstrap a board + discovery→card mapping
   │   │
   │   ├── hourly_*                    ── BRANCH: history-extraction + reconciliation pipeline (acyclic) ──
   │   │   ├── hourly_common.py        shared helpers (bottom of the chain)
   │   │   ├── hourly_extractor.py     orchestration: bucket → chunk → run; also --reconcile-only (SessionStart smart-recon, gated, .recon_state.json)
   │   │   ├── hourly_emit.py          emit ONE card
   │   │   └── hourly_reconcile.py     Haiku reconcile sweep (only_discovered flag) — runs at BOOTSTRAP + SessionStart
   │   │
   │   ├── discover2.*                 ── BRANCH: heuristic harvest ──
   │   │   ├── discover2.py            entry
   │   │   ├── discover2_sources.py    jsonl / convo / git / memory readers
   │   │   ├── discover2_extract.py    signals → tasks
   │   │   └── discover.py             legacy session-shaped fallback
   │   │
   │   ├── _* leaves                   ── LEAVES: utilities, imported BY callers, never import up ──
   │   │   ├── _boardio.py             atomic write + cross-process flock
   │   │   ├── _render.py              markdown / html export
   │   │   ├── _metrics.py             velocity stats
   │   │   └── _hook_*.py              opt-in Claude Code hooks (find-board / flash / stop-recon)
   │   │
   │   └── support                     ── LEAVES: standalone tools ──
   │       ├── digest_compact.py  measure_digest.py  regen_index.py  sweep_status.py
   │       ├── port_registry.py   ── owns ALL cross-board state in ~/.board-steward/:
   │       │                         liveness registry + sticky port-assignments + last-active pointer
   │       ├── archive_done.py    report.py       health_check.py  log_event.py
   │       └── install_*.py        per-OS autostart + hook wiring (launchd/systemd/taskscheduler)
   │
   ├── docs/                           deep-dive docs (linked from SKILL.md, read on demand)
   │   └── BOOTSTRAP.md                PART 1 — one-time bootstrap/install (History Replay, §J inline, hooks, autostart, full schema); read only when first creating a board
   └── dev/                            NOT shipped — sim/test/pipeline (smoke_test, render_session_raw, sim_*)
   ```

   **How to read the tree:** BRANCHES (`card.* serve.* hourly_* discover2.*`) own a concern; LEAVES (`_*`, support) are depended on, never depend up. Dependency flows **downward only** (branch → leaf), so there are **no cycles**. New work attaches to the branch that owns its concern, or becomes a new leaf — it never rewrites a parent.

   **Invariant:** 33 script modules, all import clean, no cycles, one-directional coupling. The 3 historical smells (oversized files, god-functions, repo↔skill duplication) are CLOSED — don't reintroduce them.

   **Extending the architecture (how future work MUST slot in — the rule that keeps it from falling apart):**
   - **Isolate, don't rewrite.** New behavior goes in the module that owns that concern, or a NEW focused module — never by restructuring a parent or rewriting a sibling that already works. A feature should be an *addition* at the right boundary, not a reshuffle of the tree.
   - **Respect the existing boundaries.** Before adding code, find which family owns the concern (`card.*` state/CLI, `serve.*` HTTP/SSE, `hourly_*` extraction, `discover2.*` harvest, `_*` leaf utils) and put it there. If it fits none cleanly, that's the signal to add a new leaf module — not to widen an unrelated one.
   - **Depend downward only.** New modules import the leaf utilities; leaves never import callers. Adding a feature must NOT introduce a back-edge or a cycle (verify: every module still imports clean).
   - **Touch the minimum.** Prefer a change that adds one file or extends one function over one that edits five modules. If a change forces edits across many parents, stop — the design is fighting you; re-scope so the change lands at a single boundary.
   - **Keep this map current.** When you do add/split/rename a module, update the map above IN THE SAME COMMIT so it never drifts from reality. A stale arch map is itself an architectural smell.
   - **Why:** the whole point is that future work never has to "rewrite parent folders." Clean isolation means each new feature is a bounded diff at one seam — that's what lets the codebase grow without falling apart.

2. **Commit ON EACH CHANGE — atomically, immediately.** After *every* meaningful change (code, docs, plan, card movement that touches files), make a separate atomic commit right then. **Never batch multiple changes into one commit, and never defer committing to "later."** Push if a remote is configured. Tag a `good-state-*` anchor before risky work so any change is reversible. If a turn produced no file changes, no commit — but say so explicitly. User's standing rule from `feedback_commit_each_change`: "make it reflexive."

3. **Dump full raw conversation at session end.** Append to today's file in the same dir. Format per `~/Desktop/conversation_history/instructions.md`. Never summarize; never overwrite.

## Shorthand aliases (user vocab)

- **`ss`** → screenshot. Always lives in `~/Desktop/ss/`. When user says "see latest ss" / "in desktop/ss", read the newest file in that directory with the `Read` tool.
- **`wb`** → WorkBoard. The kanban plugin repo at `~/Desktop/WorkBoard/`, served at `http://127.0.0.1:7892` (separate from the QuantifyMe board on `:7891`).
- **`qm`** → QuantifyMe. The trading product at `~/Desktop/QuantifyMe/HFTAgents/`, board served at `http://127.0.0.1:7891`.
