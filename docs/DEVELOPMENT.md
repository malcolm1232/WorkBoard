# Development & internals

Contributor-facing notes for WorkBoard / the `board-steward` plugin. If you just
want to *use* it, see the top-level [`README.md`](../README.md). This file covers
the repo layout, the source-of-truth split, how to run a History Replay during
dev, and how to verify/repair an install.

board-steward is packaged as a Claude Code **plugin** — it bundles the
board-steward *skill* (`SKILL.md`, the playbook Claude follows) plus the four
`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `Stop` hooks that keep the
board live. This repo is the canonical source.

board-steward has **two halves**, kept distinct:
- **History Replay** — the retrospective *bootstrapping* fill: mine a project's past `~/.claude` session history and fly cards onto a fresh board so a new user opens it already showing last week's work.
- **Live tracking** — the going-forward capture: as you work, Claude files/moves/ships cards in real time (driven by the bundled hooks).

**Token cost.** ~80 tokens of skill-list description (always-on). ~130 tokens once per session for the board digest. The full `SKILL.md` (~7.7K tokens) loads only when Claude actively engages with the board. `board.json` itself (can be 130 KB+) lives on disk and is never auto-loaded — Claude queries it via `card.py` CLI primitives that return tens to a few thousand tokens per call. See [`TOKEN_BUDGET.md`](TOKEN_BUDGET.md) for measurements + peer benchmarks (claude-mem, mem0, letta, graphify, CLAUDE.md baseline).

## What's in the box

- `SKILL.md` — the playbook Claude follows (greet → traverse → act → log → sign off)
- `scripts/` — Python helpers (stdlib-only, no dependencies)
  - `serve.py` — local HTTP server with SSE live-streaming (no File System Access API; cross-browser). Serves `/board.json`, `/events`, `/metrics`, `/tokens`, `/export.md`, `/export.html`, `/flash`, `/health`.
  - `card.py` — the CLI. Mutations (`add` / `move` / `fly` / `update` / `link` / `subtask` / `column` / `bug` / `improve` / `auto-ship`), reads (`digest` / `query` / `show` / `list` / `wiki` / `export` / `metrics` / `tokens`), and data-safety (`recover` / `migrate` / `repair-links`).
  - `_boardio.py` · `_render.py` · `_metrics.py` — shared internals (write-safety, HTML/MD renderers, velocity compute) imported by both `card.py` and `serve.py`.
  - `_tokens.py` · `_tokens_report.py` - token + API-equivalent cost tracking (#958): an incremental SQLite index of every Claude Code transcript under `~/.board-steward/tokens/`, attributed to project, session, turn and card (each number labelled exact / inferred / unattributed); `card.py tokens`, `GET /tokens` and the board's Tokens view read it.
  - `regen_index.py` — produces a small `index.json` digest for cheap Tier-1 reads
  - `archive_done.py` — sweeps Done cards older than 14d into monthly archives
  - `discover.py` / `discover2.py` / `hourly_extractor.py` — the **History Replay**: mine `~/.claude/projects/*/*.jsonl` and fly cards onto a fresh board so a new user opens the board and already sees their last week of work, animated in card-by-card
  - `digest_compact.py` — lossless token-cut applied to each History Replay digest before extraction (drops zero-signal boilerplate, never a file/commit/decision line)
  - `log_event.py` / `report.py` — Steward self-telemetry
  - `install_hooks.py` — wires the Claude Code hooks: `SessionStart` (board digest once per session) + `PreToolUse` (flashes a card when Claude edits a file linked to it)
  - `install_autostart.py` — **cross-platform** autostart dispatcher → delegates to `install_launchd.py` (macOS), `install_systemd.py` (Linux), or `install_taskscheduler.py` (Windows); identical flags on every OS
  - `health_check.py` — green/red dashboard verifying autostart + server + hook installed + hook fired
- `templates/`
  - `board.html` — the kanban UI (single-file, vanilla JS; Board / Calendar / Velocity views)
  - `board.json` — empty-board starter (6 default columns: Task / Backlog / In Progress / Done / Notes / Mandatory)
  - `tag-profiles.json` — 5 industry tag taxonomies (software / marketing / research / product / operations)

## Repo vs installed plugin (source of truth)

board-steward lives in two places, on purpose:

| | Path | Role |
|---|---|---|
| **Dev repo** | `~/Desktop/WorkBoard/` | **Source of truth.** Git history, the live dev `board/`, the `training_data/` corpus. Edit here. |
| **Installed plugin** | `~/.claude/plugins/cache/workboard/board-steward/<version>/` | The clean, standalone copy Claude Code actually loads (resolved at runtime via `${CLAUDE_PLUGIN_ROOT}`). Never edit here. |

The installed plugin is refreshed by **reinstalling from the marketplace** — Claude Code loads it from the versioned cache, so there's no manual sync:

```bash
claude plugin marketplace add ~/Desktop/WorkBoard   # register the repo as a marketplace (once)
claude plugin install board-steward@workboard       # (re)install fresh repo code
# same-version refresh? clear the cache first: rm -rf ~/.claude/plugins/cache/workboard/board-steward
```

`dev/install_git_hooks.sh` installs a **`post-commit`** hook that runs the fast regression smoke (`dev/smoke_test.py --fast`, #316) after each commit — silent on success, loud on a regression so you catch it before pushing. Run it once after cloning. (`install.sh` is the separate copy-based path for end users who don't have the repo.)

## History Replay

The **History Replay** is the onboarding demo: point it at a project's `~/.claude` chat history and it reconstructs the work as kanban cards, flying them onto a fresh empty board (`task → in-progress → done`, including real bug-bounces and improvements via `transitions[]`). It's how a brand-new user opens the board and *already* sees their last week of work instead of an empty state.

```bash
dev/simulate_install.sh --project <dir> --days N --port 7896   # run a History Replay on an isolated board
```

It is distinct from the per-card *lifecycle* walk — History Replay is the **retrospective** fill from past history; the lifecycle tracking is the **going-forward** capture as you work.

## Verify the install

```bash
python ~/Desktop/WorkBoard/scripts/health_check.py
```

Prints a green/red dashboard checking, for every registered port:

- autostart has a live PID (server auto-starts at login — launchd/systemd/Task Scheduler)
- `/health` responds with rev + card count
- `SessionStart` hook is installed in `~/.claude/settings.json`
- Hook actually fired in the most recent Claude session (greps the session jsonl for the injection marker)

Exit code 0 only when all four pass. Add `--json` for machine-readable output.

### Troubleshooting: reinstalled but `0 hooks` / board won't open

If `/reload-plugins` reports `· 0 hooks ·`, the plugin installed but isn't
**enabled** — Claude Code dropped `"board-steward@workboard": true` from
`enabledPlugins` in `~/.claude/settings.json` (a known reinstall/marketplace-churn
gotcha; the plugin can't self-enable). Run the install-doctor:

```bash
python3 ~/Desktop/WorkBoard/scripts/doctor.py        # diagnose
python3 ~/Desktop/WorkBoard/scripts/doctor.py --fix   # auto re-add the enable entry
```

Then `/reload-plugins` (or restart) → expect 7 hooks. To avoid the gotcha on a
reinstall, do a single `/plugin install board-steward@workboard` — skip the
`marketplace remove`/`add` churn (that's what strips the enable entry).

## Why this exists

Branching todos drop items. Item 1 spawns 1.1, which spawns 1.1.1, and item 5 gets forgotten three levels deep. The board makes the full tree always-visible (subtasks inside cards) and auto-updates as work moves Backlog → In Progress → Done with per-card write-ups Claude fills in when a task ships.
