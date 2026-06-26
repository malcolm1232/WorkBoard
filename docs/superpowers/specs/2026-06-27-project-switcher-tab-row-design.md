# Project switcher tab row (#841)

**Date:** 2026-06-27
**Card:** #841 — "if I have multiple projects, can I have multiple cards top-left in ONE wb?"
**Status:** Approved design

## Problem

The user now runs multiple projects, each a fully isolated board: its own `board.json`,
its own `serve.py` process, its own port (`~/.board-steward/port-assignments.json`):

- 7891 WorkBoard · 7892 DBSearch.AI · 7893 QuantifyMe · 7894 MarketingForWB

To view a different project today you must open a different port by hand. The user wants
a switcher — a tab row top-left — to jump between projects from one window.

## Decision: Option A (switcher), not a merged view

Two readings of "multiple cards in ONE wb":
- **A — project switcher**: one window that flips between the existing per-port boards.
- **B — merged view**: one board showing all projects' cards together.

We build **A**. It delivers the "switch between my projects" UX with near-zero risk
because each board stays its own isolated server/data/SSE exactly as today. B would
require a cross-project data model and touch the hardened CAS/recon/SSE code — out of scope.

## Architecture — three additive pieces

Every board server is identical, so the tab row looks the same regardless of which board
is being viewed. Nothing below mutates `board.json` or touches the CAS/recon write path.

### 1. `GET /boards` (serve.py) — read-only

Returns the list of all known projects for the tab row.

- Source of truth: `~/.board-steward/port-assignments.json` (already self-prunes missing paths).
- For each assignment, return:
  - `path` — the board dir (the assignment key)
  - `port` — assigned port
  - `title` — the raw `title` field from that board's `board.json` (or null). The tab
    row normalizes it client-side with the existing #837/#838 JS name-extraction
    function, so the normalize logic is not duplicated in Python.
  - `running` — whether its server is alive (`port_registry` pid-alive check)
- A board whose `board.json` is missing/unreadable is omitted.
- Response shape: `{"boards": [ {path, port, name, running}, ... ], "current_port": <int>}`
  ordered by port (stable).

### 2. `POST /ensure-board?path=<board_dir>` (serve.py) — spawn-only

The auto-start piece, so clicking any tab "just works".

- Look up the assigned port for `path`. Health-check `http://127.0.0.1:<port>/health`.
- If already up → return `{port, url}` immediately.
- If down → spawn detached using the exact existing pattern (see
  `hook_session_start.sh` / `bootstrap_project.sh`):
  `env -u CLAUDECODE nohup python3 serve.py --project <root> --port <port> >log 2>&1 </dev/null & disown`
  then poll `/health` until up, with a short bounded timeout (~5s).
- On success → `{port, url}`. On timeout/failure → HTTP error so the UI can recover.
- Validation: `path` must be a key in `port-assignments.json` (no arbitrary spawn).

### 3. Tab row UI (board.html, top-left)

- On load, `fetch('/boards')`; if fewer than 2 boards, render nothing (nothing to switch to).
- Render a pill per board, ordered by port; label = #837/#838-normalized `title`. Active
  pill (matches `current_port`) is filled.
- Click handler:
  1. Show a brief "starting…" state on the clicked pill.
  2. `POST /ensure-board?path=<path>`.
  3. On success → `window.location = url` (same tab).
  4. On failure → restore the pill + inline "couldn't start" toast; no navigation.
- Clicking the already-active pill is a no-op.

## Defaults chosen

- **Same-tab** navigation (natural switcher feel), not new tab.
- **Order by port** — stable, won't reshuffle as projects are added.

## Edge cases

| Case | Behavior |
|------|----------|
| Board file gone/unreadable | Omitted from `/boards` (assignments self-prune). |
| Auto-start fails within timeout | Pill restores + "couldn't start" toast; stay put. |
| Only one project exists | No tab row rendered. |
| Click active tab | No-op. |
| `ensure-board` for unknown path | HTTP 400; never spawns arbitrary processes. |

## Testing

Use the existing `/e2e` harness (spins throwaway boards on isolated ports):
- `/boards` lists the throwaway boards with correct `running` flags.
- `/ensure-board` brings a deliberately-stopped throwaway board up (health goes 1).
- The live board (`board.json` rev) is untouched throughout — pure additive endpoints.

## Out of scope

- Merged / aggregate cross-project view (Option B).
- Reordering tabs by hand, pinning, per-project colors.
- Starting boards that aren't already in `port-assignments.json`.
