# Changelog

All notable changes to WorkBoard / the `board-steward` skill.

The format follows [Keep a Changelog](https://keepachangelog.com/); this project
uses date-stamped pre-1.0 development entries until the first tagged release.

## [Unreleased]

Pre-release hardening toward `v1.0.0-rc.1`. Built across Plan v2 phases 0–6.

### 0.9.14 — Multi-part carding LAW + decompose-before-IP guard (2026-06-06)
Full record: `docs/SESSION_LOG_260606.md`.
- **Codified the multi-part carding LAW (#476, `2d037cf`).** SKILL.md shape table is
  now a LAW gated by the **header test**: one honest `verb + noun` header covers all
  parts → **1 card + N subtasks** (parts as subtasks, never in the title); no single
  header → **N cards**. Mid-task branch test clarified (serves current goal → subtask,
  else new card).
- **Enforce decompose-before-IP (#103, `71fabac`).** A fresh-install test caught an agent
  flying a multi-part card to `inprogress` with zero subtasks (parts lost). Fix: SKILL.md
  gained a 5-step ordered procedure (decompose in Task *before* IP) + HARD RULE "no naked
  multi-part card in IP"; `card.py fly` now blocks a multi-part-looking card with no
  subtasks on the task/backlog→IP hop (new `_looks_multipart` heuristic; override `--force`
  / `BOARD_SKIP_DECOMPOSE_CHECK=1`).
- **Recon-gate try/finally (#384, `26d58fb`).** End-of-replay reconcile wrapped so the
  replay gate always reopens even if the sweep raises — prevents SessionStart recon from
  being permanently skipped on a board (regression from 23fdc02). Live-validated during
  this session's bootstrap.

### 0.9.13 — Bootstrap reconcile: kill premature COMPLETE + recon race; number-free reconcile HUD (2026-06-05)
- **No premature "✓ COMPLETE".** The tier-fly speedup/solo tier wrongly set
  `is_final=True` (recon runs outside the window, so it was called `reconcile=False`),
  flashing COMPLETE + auto-hide before the end-of-replay sweep re-showed the HUD. New
  `will_reconcile` threaded `_run_window → _extract_haiku` makes the last tier hand off
  to RECONCILING on the same HUD.
- **Exactly one reconcile (race killed).** `_mark_replay_complete()` flipped the replay
  gate *before* the sweep, letting a SessionStart `--reconcile-only` race it; nothing
  serialized recon. Now: reconcile FIRST then flip the gate (gate stays closed for the
  sweep → recon-only stands down), plus new `_boardio.recon_lock` (bail-if-held flock on
  `board/.recon.lock` → any second concurrent reconcile skips). Fixes the
  "already up to date → cards shuffle → N up to date, twice" report.
- **Number-free reconcile HUD.** Reconcile is one LLM sweep, not N chunks, so its N/M +
  % counter was meaningless and laggy (stale `8/8` then `1/1`). Hidden via a `.lh-recon`
  class during `phase=='reconcile'`; the bar + blinking `▶ RECONCILING` still convey
  activity. Browser test +3 assertions → 20 checks.

### 0.9.12 — Lean HUD: drop the tail line, shorten reconcile copy (#78, 2026-06-05)
- **Removed the redundant bottom tail line** (`#lh-tail`, e.g. "✓ chunk 2/7") from
  **every** HUD state — it duplicated the window line and showed a differently-based
  count that didn't tally with the headline. DOM element, the JS that wrote it, and
  the now-unused `.lh-tail` CSS all removed. The HUD is now header → status+count →
  one window line → bar.
- **Shortened the reconcile copy** — "catching the board up so nothing shipped or
  important is missed" (62 chars) overflowed the ~330px window and cut off
  mid-sentence; the in-progress line is now the present-tense action
  **"checking nothing's missed…"** (the *outcome* "nothing missed" stays on the ✓
  final line; the header already says "reconciling"). Browser test now asserts the
  tail is absent (17 checks).

### 0.9.11 — Single coherent BOARD-LOAD HUD (#78, 2026-06-05)
- **One HUD across all three fill stages, no race.** The bootstrap fly-in HUD used
  to **complete + auto-hide then reappear** between stages — because the reconcile
  sweep ran at the end of **every** tier (`replay` AND `speedup` both got
  `reconcile=True` from the shared `common` dict), and each reconcile hitting
  `done>=total` made the frontend flash "✓ COMPLETE" and start its 6s hide timer,
  only for the next tier to re-show it. Now reconcile runs **exactly once**, after
  the final tier (`replay` tier → `reconcile=False`), and the HUD flows
  `replaying last 24h → speeding up ▸▸ → reconciling → ✓ COMPLETE` as a single
  persistent panel that never disappears mid-flow.
- **Completion is backend-driven, not guessed.** A new `final` flag on the
  `card.py progress` payload (plumbed through `serve.py`, `_emit_progress`,
  `_banner_update`, and the reconcile terminal emits) marks the genuine end —
  only that triggers `done()`. Intermediate stage-ends `handoff()` instead
  (stay visible, advance the header). `serve.py` now drops the replay-cache on
  `final` (not on `done>=total`), so a reconnecting client still sees an
  in-progress 100% handoff.
- **Count is 1-based — starts at 1/N, ends at N/N.** The readout used to start at
  `0/N` and freeze at `N-1/N` (the "ends at 6/7, never 7/7" bug) because
  `handoff()`/`done()` set the bar to 100% but never wrote the count. Now the
  headline shows the 1-based current item (`min(done+1,total)`) and `handoff`/`done`
  write `N/N`; the tail no longer repeats a differently-based "chunk N/M".
- Browser-verified end-to-end (`dev/test_hud_single.py`, 16 checks via a real
  server + real EventSource + chromium).

### 0.9.10 — Subagent card-tracking mode dial (2026-06-05)
- **Stop auto-carding every sub-agent as a top-level card (#79)** — the subagent
  hooks previously created one top-level card per spawned agent, which polluted the
  board (5 orphan "Simulate …" cards from internal tooling agents). New **mode dial**
  resolved per board (`BOARD_SUBAGENT_CARDS` env → `board.settings.subagentCards` →
  default): **`off`** (no tracking) · **`subtask`** *(default)* — a sub-agent's work
  becomes a **subtask of the active In-Progress card**, or **nothing** if none is in
  flight (internal helpers stop polluting) · **`collab`** — opt-in for agent-to-agent
  product builds: each sub-agent gets its **own child card linked to an epic**
  (`settings.subagentEpic`), so the board mirrors the agent tree. Read-only types
  (Explore/Plan) still never carded. Granularity rule added to SKILL.md ("a top-level
  card is for USER-named work, not your mechanics"); dial documented in BOOTSTRAP.md.
  Cleaned up the 5 orphan sim cards (#68–72).

### 0.9.9 — Stop-backstop false-positive fix (2026-06-05)
- **Two false-positive classes killed (#78, `<this commit>`)** — the blocking
  un-carded backstop no longer fires on (1) turns that only edit files **outside**
  the board project (e.g. `~/.claude` memory files, another repo) — edits are now
  scoped to `project_root`; or (2) the **cross-turn** carding pattern (`fly inprogress`
  in turn N, edits in N+1, `fly done` in N+2) — an existing In-Progress card now counts
  as the unit being declared, so the edit-heavy middle turn isn't blocked. Genuine
  misses (in-project edits, no `card.py`, no rev bump, nothing in flight) still block.

### 0.9.8 — LIVE-protocol de-dilution + live-carding enforcement (2026-06-05)
- **SKILL.md LIVE section de-diluted (#73, `7317709`)** — replaced the generic
  7-step `add→fly` list with **three laws** (declare-don't-record · one-pulse-at-a-
  time · the Stop hook can't gate batching) + a **shape→pattern table** covering all
  five work shapes (single unit / multiple to-dos / plan mode / phase-tier / mid-task
  branch). Derived from 5 parallel simulations of the proper-carding outcome.
- **Stop hook: batched-not-live detector (#74, `31ab943`)** — `detect_batched()`
  flags cards that reached Done this session with no in-flight dwell (Task→Done jump,
  or <30s in In-Progress), using `card.history` events and scoped to the window since
  the last Stop. **Non-blocking** advisory — surfaces the add→done smell the rev/marker
  checks were structurally blind to. Paired SKILL.md Law #3 rewrite.
- **card-before-edit PreToolUse WARN hook (#75, `9683820`)** — new
  `_hook_card_before_edit.py`: on an edit inside a board project with NO In-Progress
  card, injects a non-blocking `additionalContext` reminder to declare the unit first
  (law #1). Never blocks; conservative + 60s-debounced. Wired into `hooks.json`,
  `install_hooks.py` (in the `all`/`live` set; `--uninstall` removes it), `clean_slate.sh`
  (+ the previously-uncleaned `.stop_recon_state.json` sidecar), and BOOTSTRAP.md.
- **SKILL.md #5 clarified as the explicit exception (#76, `aa3a373`)** — the mid-task
  branch row now states up front it's the one shape where new work nests as a *subtask*,
  not a new card (resolving the apparent contradiction with the "new work = new card" rows).
- Module invariant: 33 → **34** script modules (the new hook leaf), all import-clean, no cycles.

### Changed — autonomous fill is the default (2026-05-31)
- **`--bootstrap-mode` / `install.sh --fill` default flipped `inline → haiku`** —
  a fresh install now fills the board **autonomously** (no main-Claude step), the
  "npx-install just works" experience. It uses the user's existing Claude login
  via `claude -p` — **no API key**. `inline` stays as an opt-in (free, full
  context, highest quality, but waits on a live session to emit).

### Fixed — haiku fill: speed, robustness, demo auth (2026-05-31)
- **`MAX_THINKING_TOKENS=0`** (`f36a25b`) — the haiku-fill slowness was extended
  *thinking* tokens (~5k/call → ~50s), not card verbosity, MCP, or chunk size.
  Forcing them off cut a full harvest **209s → 34s (~6×)** with identical quality.
- **Robust JSON salvage** (`5327920`) — `parse_card_array` recovers cards from
  prose-wrapped or truncated model output (jsonl digests carry chat turns that
  derail Haiku into prose), eliminating the 90s-timeout retry cascade.
- **`--demo` haiku auth** (`e09fb68`) — the isolated demo config dir broke
  `claude -p`; `_LLM_ENV` now honors `BOARD_REAL_CLAUDE_CONFIG_DIR` so every
  `claude -p` call (harvest + `serve.py` bootstrap) authenticates against the
  user's real login instead of filling 0 cards while printing "fill complete".

### Added — auto-logging (Phase 3, the VISION "zero-input" promise)
- **Auto-card on idea-intent** (`#100`) — `card.py add --auto`; deferred-intent
  markers in a prompt create a card with a 5-second Undo toast.
- **Auto-ship after commit** (`#101`) — `card.py auto-ship` scores In-Progress
  cards against `git log` and writes the completion summary from matched commits.
- **Auto-link files to cards** (`#102`) — a `PreToolUse` hook flashes a card on
  the board when Claude edits a file linked to it (`/flash` SSE endpoint).

### Added — data-safety (Phase 3.5)
- Cross-process `flock` + rolling backups on every write (`_boardio.py`).
- `card.py recover` — list / restore rolling backups (validated, reversible).
- `card.py migrate` — idempotent, `schemaVersion`-driven schema migrations.
- `card.py repair-links` — fix dangling / self / duplicate / one-sided links.

### Added — cross-platform autostart (Phase 4)
- `install_autostart.py` dispatcher → `install_launchd.py` (macOS),
  `install_systemd.py` (Linux), `install_taskscheduler.py` (Windows). Identical
  flags on every OS; unprivileged; refuses a real install on the wrong OS.

### Added — token-efficiency read tier (Phase 5)
- `card.py digest [--json]` — ~120-token board pulse on demand.
- `card.py query` — sliced JSON; `--fields` projection, `--since-days`, `--limit`.
- `card.py wiki` — narrative Markdown render.
- SKILL.md documents the `digest → query → show → board.json` ladder.

### Added — scale + share (Phase 5.5)
- **Export** (`#115`) — `card.py export` and `serve.py /export.md` / `/export.html`
  produce a standalone, no-JS sprint snapshot. Shared renderer in `_render.py`.
- **Velocity metrics** (`#114`) — `serve.py /metrics?since=Nd`, `card.py metrics`,
  and a Velocity tab in the UI (throughput, cycle time, blockers, priority drift).
- **LAN access + auth** (`#116`) — `serve.py --auth-token`; bearer token via
  `Authorization` / `?t=` / cookie, constant-time compare; prints a scan-me LAN
  URL. `card.py` carries `$BOARD_AUTH_TOKEN` on its writes.

### Pending before `v1.0.0-rc.1`
- `#113` lazy-render + incremental SSE diff for 500+ card boards (Phase 5.5, deferred).
- `#112`/`#245` full-text / Cmd+K search.
- `#247` inline hourly transition extractor.

## [0.1.0] — 2026-05-26
- Initial commit — WorkBoard kanban skill extracted from `board-steward`:
  live SSE board (`serve.py` + `board.html`), `card.py` CLI, `index.json`
  digest, archive sweep, history bootstrap (`discover.py`), SessionStart hook,
  launchd autostart, self-telemetry.
