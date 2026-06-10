# board-steward — Key Features

A live kanban work-board built **for Claude Code agents and the humans they work with**. The board is the source of truth for active work, and — crucially — **the agent keeps it live without being asked.** Most "agent memory" tools are passive stores you have to remember to write to. board-steward is enforced: hooks make forgetting to track work a self-correcting event, not a silent drift.

---

## 1. Hook-enforced live tracking — the headline feature

The board can't drift, because four Claude Code hooks keep the agent honest in real time. This is what separates board-steward from every "the agent *should* update a file" tool.

| Hook | Event | What it does |
|---|---|---|
| **SessionStart** | session start | Injects a ~150-token board digest; re-spawns the server if the port is dead. |
| **UserPromptSubmit** | every prompt | Injects the live-lifecycle protocol so the agent cards work as it goes, never batched. |
| **PreToolUse (card-before-edit)** | before an `Edit`/`Write` | **Non-blocking warn** — if the agent is about to edit a file with no card In-Progress, it nudges "declare a card first." Agent-facing only; never shown to or blocking the user. |
| **Stop (sign-off backstop)** | agent tries to end its turn | If the turn made real edits/ships but ran no `card.py`, it records the gap. **Advisory by default** (silent note, caught next session); **opt-in strict mode** forces the agent to card it the same turn. |

### How the Stop backstop actually behaves (the important nuances)

- **Advisory by default — invisible and free.** Out of the box the hook just writes a `recon_pending.json` note and exits; the next SessionStart surfaces any genuine un-carded miss for reconciliation. **Zero model tokens, nothing the user ever sees.** Power users who want a hard same-turn guarantee set `BOARD_STEWARD_STRICT=1`.
- **Strict mode blocks the *agent*, not the user.** "Block" is the Claude Code term for "don't end the turn yet" — it loops the *agent* back to card the work. The human is never interrupted or asked to do anything. (Costs one extra model turn when it fires, which is why it's opt-in.)
- **Even strict mode is a single-shot nudge, not a wall.** A loop-guard (`stop_hook_active`) lets it fire *at most once* per stop. On the forced continuation the agent can comply **or decline with a one-line justification and stop** — it cannot re-block, so a false positive can never trap the agent in a loop.

The net effect: the user **never has to ask "did you update the board?"** — and that question is the exact failure mode the whole project exists to kill.

---

## 2. Zero-input auto-logging

Work logs itself, with no "want me to add this?" prompts:

- **Idea → card.** User says "I have an idea: X" → a card animates into Ideas in real time (with a 5s Undo toast).
- **Start → In Progress.** Agent begins implementing → card glides to In Progress, pulses, and pins to the top.
- **Ship → Done.** Agent finishes → `card.py fly <n> done` writes a multi-paragraph completion write-up (commit SHAs, files touched, verification) and the card glides to Done.
- **Commit → auto-ship.** A git commit auto-ships the matching card via `git log` scoring.
- **Branch → subtasks.** Mid-task branching trees out *inside* the card; the parent never leaves the screen.

---

## 3. Token-efficient by design

Most agent tools dump 100KB+ of state into context every turn and call it memory. board-steward is the opposite — a **progressive-disclosure ladder**:

- **~150-token digest** at session start (`🚨 SUPER URGENT: 3 · In Progress: 2 · Done: 392 · Last shipped #589`).
- **CLI primitives** — `card.py digest` → `query` (sliced JSON) → `show <n>` (one card) → `board.json` (last resort). The big file (130KB+) is **never auto-loaded**.
- **`index.json`** — a compact digest regenerated atomically after every write for the rare sweeping view.
- Target: **<2KB of context** on a typical session start, vs ~147KB for a naive design. Measured, not aspirational (see `docs/TOKEN_BUDGET.md` for peer benchmarks vs claude-mem, mem0, letta).

---

## 4. Live, animated UI — a dashboard, not a database

- **Live motion over SSE.** Cards pop in with overshoot easing; cards moving columns animate with the FLIP technique — they *glide*, not teleport. Cross-browser (no File System Access API).
- **Glanceable, not clicky.** Origin ("why this card exists") on hover; time-since-update inline; priority as a color stripe; code badges for shared vocabulary.
- **Real calendar tab** — cards land on their done/created day; "we shipped 17 things on May 25" at a glance.
- **Velocity view** — throughput, cycle time, blockers.
- **No setup screens.** Open the URL, the board is there.

---

## 5. History Replay — open the board already full

The onboarding move no other kanban can do: point it at a project's `~/.claude` chat history and it **reconstructs past work as cards**, flying them onto a fresh board (`task → in-progress → done`, including real bug-bounces and improvements). A brand-new user opens the board and *already sees their last week of work*, animated in card-by-card — instead of an empty state to fill.

---

## 6. Instant, invisible startup

- **Cross-platform autostart** — `launchd` (macOS) / `systemd` (Linux) / Task Scheduler (Windows), one dispatcher, identical flags. The local server is up at login; `http://127.0.0.1:7891` just works.
- **Hook fallback** — if autostart dies, the SessionStart hook detects the dead port and re-spawns the server in the same turn. The user never sees a broken board.
- **One command** to install in any project: `python serve.py --bootstrap`. No config, no account, no cloud.

---

## 7. Data-safety & sharing

- **Crash-safe writes** — cross-process `flock` + rolling backups on *every* write.
- **Repair CLI** — `recover` / `migrate` / `repair-links` to restore, evolve schema, and fix broken links.
- **Share + glance** — `export` to standalone HTML/Markdown for a sprint recap; optional bearer-token auth (`--auth-token`) to glance on your phone over the LAN.

---

## Design principle

> **Zero input from the user. Work auto-logs.**

The human *glances* at the board — never types, drags, or configures — and knows the full state of the collaboration. Everything else is enforcement machinery to make that true even when the agent is three branches deep and would otherwise forget.
