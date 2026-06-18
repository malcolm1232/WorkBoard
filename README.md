<div align="center">

# 🗂️ WorkBoard

### A live knowledge graph of your work.

**Never lose an idea. Never lose a workflow.**

![Version](https://img.shields.io/badge/version-0.9.30-blue) ![License](https://img.shields.io/badge/license-Apache--2.0-green) ![For Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2) ![Runs locally](https://img.shields.io/badge/runs-100%25%20local-success) ![No account](https://img.shields.io/badge/account-none-lightgrey)

![Watch a fresh board fill itself — History Replay flying past work onto the board on install](docs/assets/workflow-bootstrap.gif)

</div>

---

## Quick start

Install from the plugin marketplace inside Claude Code:

```
/plugin marketplace add malcolm1232/WorkBoard
/plugin install board-steward@workboard
```

Prefer to install from source? `git clone` it and run `./install.sh` — same result, no marketplace step:

```bash
git clone https://github.com/malcolm1232/WorkBoard
cd WorkBoard
./install.sh   # sets up + auto-detects your projects and bootstraps a board
```

**Requirements:** Claude Code · Python 3.9+ (standard library only, no `pip install`) · macOS / Linux / Windows. **No account, no cloud, no API key required.** (History Replay's optional bootstrap uses Claude Haiku — the cheapest tier — as a one-time, detached subprocess.)

---

## The problem

| # | Problem | How WorkBoard solves it |
|---|---|---|
| 1 | We **can't keep track of our code**. | WorkBoard **tracks live what Claude is working on** and what it just shipped — without you typing anything. |
| 2 | You're **generating ideas faster** than you can act on them. | Card them on the spot — as cards or subtasks. Later, just say *"Do #426"* — or chain them: *"Do #426, #123 and #99."* Each one picks up exactly where it was left. |
| 3 | You're getting more done than ever — but **how do you remember what shipped**, why, which files? | Every card has a **title** (the gist) and a **short description**; the deeper work (subtasks, writeup, files, commits) hangs off as leaf nodes. Future Claude reads a tiny digest first and **traverses only the leaves it needs** — answering "what did we do on OAuth in May?" costs a handful of tokens, not a re-read of every chat log. |

## The workflow

1. **Every Task is Captured.** Before any work begins, the request is captured as a card in the **Task** column.

2. **Immediately know what Claude is working on.** The moment Claude starts working on it, the card glides into **In Progress** and pulsates — so at a glance you know exactly which card it's on. *Working on multiple projects at once? WorkBoard knows which board belongs to which project and updates each accordingly.*

    <img src="docs/assets/inprogress-pulsating.gif" width="360" alt="A card pulsating in the In Progress column — at a glance, you can see which task Claude is currently working on, even with multiple sessions running">

3. **Shipped → Done, with a write-up.** Once finished, Claude writes a description of **what was done**, **why this problem existed in the first place**, and ✓ a write-up of **how it was done**. The card flies to Done.

4. **Bug? Back to In Progress.** If something breaks — or more changes are needed — the card animates **back out of Done** with a `🐞 bug` tag, or with an added subtask if it's just a follow-up.

5. **Re-shipped → Done.** Once fixed, the card returns to Done — with the full ship → bug → fix → ship arc preserved in its history… **ready for traversal**.

---

## Features

### 1. 🏷️ Filter by tag — find what you need fast
When a card is created, it's automatically tagged with the work-type it belongs to (e.g. `UI`, `security`, `bug`, `refactor`). Click any tag chip to filter the board down to only that slice — answering *"what's open on the UI side right now?"* in one click.

<img src="docs/assets/tags-filter.gif" width="320" alt="Clicking a tag chip filters the board down to only cards carrying that tag — UI, security, bug, etc.">

### 2. 📅 Calendar View
See what shipped — and what's **still open** — laid out by date. Catch missed work from yesterday, spot productive streaks, or look back at your wonderful week of progress at a glance! You can use it to show your boss what a Teacher's pet you've been (or not).

![Calendar view — what shipped, day by day, with what's still outstanding](docs/assets/calendar-view.gif)

### 3. ✅ Subtasks track the real work, step by step

**The live tick.** Each card breaks down into the steps the agent will actually take. Subtasks tick off one by one as the work progresses — so even mid-task you can see exactly how far along Claude is (e.g. *2/4*), not just *"in progress."*

![Subtasks ticking off incrementally as Claude works through them — 1/4, 2/4, 3/4, 4/4](docs/assets/subtasks-incremental.gif)

**Anatomy of a card.** Subtasks are just one slot. Here's every field that hangs off the title:

- **Origin** — why this card exists, in the user's own words.
- **Subtasks** — the concrete steps Claude will (or did) take, ticked off one by one.
- **Notes** — anything jotted along the way (reasoning, dead ends, decisions).
- **Tags** — work-type chips (`UI`, `security`, `performance`, `infra`, `docs`, `bug`, `refactor`…) — click any to filter the board.
- **Priority** — a `C` / `M` / `L` chip in the corner (Critical / Mid / Low, or unset).
- **Linked files** — auto-attached the moment Claude edits a file under the card. Walks both ways: from card → which files, from a file → which card touched it.
- **Linked cards** — explicit `card.py link <a> <b>` edges between related work. This is the *graph* in "knowledge graph."
- **Write-up** — added when the card flies to Done: what shipped, why, and how it was verified (commits, files, tests).

![A card with subtasks expanded — every step of the work, visible at a glance](docs/assets/actual-card-subtasks.png)

### 4. `🐞 bug` — The card flies back out of Done, full history kept
**Debugging?** As you (or Claude) is working, it animates back out of Done into In Progress with a `🐞 bug` tag and a new subtask for the fix. The card's history shows the entire ship → bug → fix → ship arc, **so the story is never lost.**

<img src="docs/assets/bug-to-and-fro.gif" width="480" alt="A card moving from Done back to In Progress with a bug tag, getting fixed, then returning to Done">

---

## 🤔 How is this different from claude-mem / mem0 / letta?

**They remember your conversations. WorkBoard remembers your _work_.**

> **mem0, claude-mem, and Letta store memory. WorkBoard is a structured knowledge-graph of what you shipped.**

Those tools embed past chat into a vector store and recall fuzzy chunks by similarity. WorkBoard records the **outcomes** as a graph you can walk. Measured head-to-head on real history — same corpus, same tokenizer ([**full receipts**](Research/token_comparison/MASTER_SUMMARY.md)):

| What you're doing | WorkBoard | Memory tools (mem0 · claude-mem · Letta) | Winner |
|---|---|---|:--|
| **Build** the memory | deterministic hourly digests, few calls | an LLM reads & compresses **every session** | 🟢 **WorkBoard — ~98–99% fewer tokens** |
| **Persist** as you work | inline carding, **0 model calls** | an LLM call **every session** (Letta: every *turn*) | 🟢 **WorkBoard — free** |
| **Run** the whole loop *(per project)* | structured + free writes | per-session / per-turn LLM tax | 🟢 **WorkBoard — 34–81% fewer tokens** |
| **Recall** a single fact | a structured card (title · origin · writeup · links) | a fuzzy vector chunk | mem0 / Letta leaner; WorkBoard beats claude-mem **25.9%** |
| **Read it yourself** | a live kanban — what shipped, why, what's open | opaque embeddings | 🟢 **WorkBoard** |

The 130 KB+ `board.json` is **never auto-loaded** — context stays clean no matter how big the board grows.

**Honest:** for vague, cross-project *"what did I once say about X?"* recall, a vector memory wins — and nothing stops you running both. Where each wins, and when to pick which: **[`docs/COMPARISON.md`](docs/COMPARISON.md)** and the full study in **[`Research/token_comparison/`](Research/token_comparison/MASTER_SUMMARY.md)**.

---

## Under the hood

### 🔒 Hook-enforced — the board literally can't drift
Four Claude Code hooks keep the agent honest in real time, so tracking isn't a thing the agent *should* do — it's a thing that *happens*:

| Hook | Fires on | What it does |
|---|---|---|
| **SessionStart** | session start | Injects a ~220-token board digest; re-spawns the server if the port died. |
| **UserPromptSubmit** | every prompt | Re-injects the live-lifecycle protocol so work is carded as it happens, never batched at the end. |
| **PreToolUse** | before an Edit/Write | Non-blocking nudge — about to edit a file with no card In-Progress? "Declare a card first." |
| **Stop** | agent ends its turn | Made real edits but ran no `card.py`? It records the gap so the next session reconciles it. Advisory by default (0 tokens, invisible); opt-in strict mode forces same-turn carding. |

The net effect: **the user never has to ask "did you update the board?"** — and that question is the exact failure mode WorkBoard exists to kill.

### Core components

- **Local board server** at `127.0.0.1:7891` — pure Python stdlib, no framework, no dependencies. Serves the animated UI, a live SSE event stream (`/events`), and a tiny REST surface (`/progress`, `/health`, `/rev`). Auto-managed by `launchd` (macOS) / `systemd` (Linux) / Task Scheduler (Windows); the SessionStart hook respawns it if it dies.

- **`board.json` — a knowledge graph in JSON, not a vector DB** — single file per project; cards (nodes) + history/subtasks/links (typed edges). Atomic writes via cross-process `flock`, rolling `.backups/` directory on every write. Deliberate choice over SQLite/Chroma: it's readable, diff-able, copy-pasteable between machines, and git-friendly. The 130 KB+ file is **never auto-loaded** — Claude reads a ~220-token digest first and traverses only the cards it needs.

- **`card.py` — the single canonical write path** — `add`, `fly`, `subtask`, `bug`, `improve`, `show`, `list`, `query`, `digest`. Hooks enforce its use; agents that try to edit `board.json` directly are caught by the Stop backstop and surfaced next session.

- **Live SSE updates** — every write broadcasts a typed event (`card-added`, `card-updated`, `card-removed`, `card-flash`, `column-*`, `rev-bumped`). All open browser tabs and Claude sessions sync in real time, **no polling**.

- **Multi-Claude, multi-board, multi-tab** — every Claude session opens its own `?sid=`-bound tab. Each pulses its **own** active card (`state.activeWork = {sid: {cardId, ts}}`): N concurrent sessions = N concurrent pulses, no fight over a single "current task." A port registry tracks which board belongs to which project; **rev-as-CAS** (`#609`) prevents lost updates when two sessions write the same card simultaneously.

- **Reinforcement: advisory by default, strict on demand** — the Stop hook's sign-off backstop is silent and free (0 tokens, just writes a note). Power users opt in via `BOARD_STEWARD_STRICT=1` — same-turn enforcement that loops the agent back to card the work before ending its turn. Single-shot, with a hard-coded escape so a false positive can't trap the agent.

- **History Replay — fly past work onto a fresh board** — on first run, a detached **Haiku** subprocess (cheapest tier) mines your past Claude Code sessions and reconstructs them as cards, flying them onto the board `task → in-progress → done`, complete with bug-bounces. Runs out-of-band so it never enters the interactive session's context.

- **Crash-safe by construction** — `flock` + rolling backups + a `recover` CLI to restore from any backup; `repair-links` to fix broken cross-card references; `migrate` to evolve the schema. Three months in, your board self-heals.

- **Token-cheap by design** — the lightest per-prompt of the five peer memory tools benchmarked. See [`docs/TOKEN_BUDGET.md`](docs/TOKEN_BUDGET.md) for the measurements, and [`docs/COMPARISON.md`](docs/COMPARISON.md) for the knowledge-graph-vs-memory-store framing in full.

---

## Optional Installation

**On first run**, WorkBoard reads through your recent Claude Code conversation history to work out what you've been working on, then **prompts you to pick which board to build**. By **default it looks back 2 days**. You can ask for a wider window — e.g. *"build a board from the past 7 days"* — to pull N days of history.

> ⚠️ **Going far back isn't recommended.** The further back you reach, the more work it finds — and you can end up **overpopulated with cards**. The default stays conservative at **2 days** for that reason; widen it only when you actually want a bigger backfill.

---

## Learn more

- [`docs/KEY_FEATURES.md`](docs/KEY_FEATURES.md) — the full feature tour
- [`docs/TOKEN_BUDGET.md`](docs/TOKEN_BUDGET.md) — measured token cost vs. peer memory tools
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — repo layout, internals, and contributing
- [`CHANGELOG.md`](CHANGELOG.md) — release history

---

## License

WorkBoard is licensed under the **Apache License 2.0**.

Apache-2.0 was chosen deliberately. WorkBoard isn't a standalone app — it embeds into your developer workflow, sits inside other people's repos, and runs as a Claude Code plugin that production tooling may rely on. For a primitive like that, the explicit **patent grant** Apache-2.0 provides (which MIT lacks) is the more responsible default: contributors can't later assert patents against the code they shipped, and downstream users get a clear, enterprise-friendly license that's broadly accepted in dev-tool ecosystems (MCP servers, IDE plugins, agent harnesses).

It remains a permissive license — commercial use, modification, and redistribution are all allowed, with attribution preserved.

See [`LICENSE`](LICENSE) for the full text. WorkBoard runs 100% on your machine; your boards and chat history never leave it.
