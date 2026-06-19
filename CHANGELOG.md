# Changelog

All notable changes to WorkBoard / the `board-steward` skill.

The format follows [Keep a Changelog](https://keepachangelog.com/); this project
uses date-stamped pre-1.0 development entries until the first tagged release.

## [Unreleased]

Pre-release hardening toward `v1.0.0-rc.1`. Built across Plan v2 phases 0–6.

### 0.9.36 — Natural-language recall: `card.py recall` (BM25F) + `/check` skill (2026-06-19)

- **`card.py recall "<words>"`** — deterministic natural-language search over the
  board. Upgraded the matcher (`scripts/text_search.py`) from the provisional
  lexical scorer to **BM25F** (field-weighted Okapi BM25 + exact-literal / `#ref`
  boosting), chosen by a measured bake-off (recall@k + tokens-per-correct over 20
  gold queries on a frozen 533-card snapshot). Wins **pinpoint hit@5 = 1.00** at
  **~268 tokens per correct recall** — zero embeddings, zero vector DB, zero API,
  zero model call. `--traverse` walks `linkedCards` for multi-card lifecycle
  recall; `--json` for machine output. (#781/#782)
- **`/check` skill** — human-facing front door to `card.py recall` (auto-discovers
  from `skills/`). "/check the auth redirect bug" → top matching card #s. (#785)
- **Validated triggering** with two agent experiments (12 agents): a
  self-documenting command name + one SKILL.md action-row reliably get agents to
  recall from the board (not memory) — no forcing hook needed. (#785)
- **Benchmarked** against mem0 / claude-mem / Letta / graphify retrieval
  algorithms and the global BEIR / LOCOMO benchmarks (our BM25 core reproduces the
  canonical BEIR BM25 nDCG; dense wins prose/chat semantics, lexical wins the
  literal-heavy work-ledger). Reproducible harnesses + reports under `Research/`.
  (#784)
- Code-review hardening: fixed a `--traverse` crash on numless linked cards and a
  `score()` wrong-card fallback; dropped a dead import. (#781)

### 0.9.32 — Quiet bootstrap: no session-refresh clutter, no stuck pulse (2026-06-18)

- **No "session refresh" dividers during bootstrap.** The Logs HUD persists to a
  *global* (cross-board, 7-day) localStorage key, so a fresh first-run board
  replayed every prior session's `──── session refresh ────` divider. Now the
  buffer is cleared once on the first bootstrap-fill tick
  (`replay`/`speedup`/`solo` phase — bootstrap-only), and the SessionStart hook
  skips emitting a new divider while `just_bootstrapped=1`. Live sessions still
  draw the divider normally.
- **The last bootstrapped card no longer pulses forever.** Cards flown into In
  Progress during the fill left `state.activeWork` pointing at the last one, so it
  kept pulsing. On bootstrap completion (`final` after a fill) `activeWork` is now
  cleared so nothing pulses. Tied strictly to bootstrap — live carding re-claims
  the pulse on the next real move into In Progress (`_set_active_work`).
- Files: `scripts/hook_session_start.sh`, `templates/board.html`.

### 0.9.31 — Laptop-fit default layout + 92% bootstrap zoom (2026-06-18)

- **Narrower default column layout so "Done" stays on-screen on a laptop.**
  Bootstrap and *Reset Columns* now arrange columns as
  `Task+Notes · Backlog+Ideas · 🚨 Super-Urgent+Discarded · In Progress · Done`
  — five horizontal columns with three vertical stacks. Notes stacks under Task,
  Ideas moves under Backlog, and Super-Urgent becomes its own column ahead of In
  Progress. Applied consistently across the seed template
  (`templates/board.json`), the migration defaults (`serve.py _DEFAULT_COLS`),
  and the *Reset Columns* SPEC (`board.html`).
- **Default zoom is now 92% on a fresh board.** `_ZOOM_DEFAULT = 0.92` is the
  fallback when no zoom is saved yet, so all columns fit out of the box; the
  Ctrl/Cmd+0 reset and *Reset Columns* both snap back to 92% (was 100%). A
  user's own zoom adjustment is still persisted.

### 0.9.30 — Calendar filter logic + header overlap fix (#722/#731) (2026-06-17)

- **Calendar filters now AND between groups, OR within a group (#722).** The
  filter chips were a flat OR, so selecting *Done + Critical* surfaced every
  done card plus every open-critical card — the Critical chip looked like it
  didn't register. Now status (`done`/`open`) and priority
  (`critical`/`mid`/`low`) are separate groups: OR within a group, AND between
  them, so *Done + Critical* shows only done-critical cards. Centralized in one
  `_calCardMatchesFilter()` helper; dropped the quirky `column !== done` guard
  so Critical alone now includes done cards too.
- **Header title no longer overlaps the toolbar buttons on narrow windows
  (#731).** The dead-center absolutely-positioned title floated on top of the
  right-side buttons once the window got narrow. Below 860px the title now
  drops into normal flow, left-aligns, and ellipsis-truncates instead of
  colliding; wide screens keep the centered look.

### 0.9.29 — Priority chip first-click fix (#683) (2026-06-17)

- **Priority chip cycles on a single click again (#683).** The `unset → C → M
  → L → unset` cycle in `makePrioChip` was correct, but `handleCardUpdated`'s
  unconditional `Object.assign(state.cards[idx], card)` ran on every SSE
  `card-updated` event, including those arriving during the 400 ms
  `scheduleSave` debounce after a local click. If the server's payload carried
  the pre-click priority, the merge reverted the local change — looking like
  "first click did nothing." Fix mirrors the existing `_localMoveLog` precedent
  (#518): a per-card `_localEditLog` timestamp lets `handleCardUpdated` strip
  `priority` from the incoming payload for 2 seconds after a local click;
  other fields merge normally. The in-place re-render now reads from merged
  `state.cards[idx]` instead of the raw SSE payload, so the protected priority
  isn't visually re-painted with the stale server value.

### 0.9.28 — README showcase + Apache-2.0 license + small UX fixes (2026-06-17)

- **License: MIT → Apache-2.0.** Deliberate switch for a primitive that
  embeds in others' workflows — the explicit patent grant matters in
  dev-tool ecosystems (MCP servers, IDE plugins, agent harnesses). Full
  Apache-2.0 boilerplate in `LICENSE`; `README` License section explains
  the choice.
- **README is now a real product showcase.** Animated demos on the landing
  page: bootstrap → History Replay (`workflow-bootstrap.gif`), live
  In-Progress pulse (`inprogress-pulsating.gif`), the bug round-trip
  (`bug-to-and-fro.gif`), and subtasks ticking off (`subtasks-incremental.gif`
  + `actual-card-subtasks.png`). Story flows in product-narrative order:
  live tracking → bug lifecycle → granular progress.
- **`docs/COMPARISON.md` (new).** Honest WorkBoard-vs-claude-mem
  comparison: the "knowledge graph of work" vs "memory store" framing,
  decision matrix, and an explicit "where each genuinely wins" section.
  Linked from README.
- **`docs/README.md` index** updated to surface user docs (KEY_FEATURES,
  TOKEN_BUDGET, COMPARISON, BOOTSTRAP, DISCOVERY, PLAYBOOK, DEVELOPMENT)
  separately from internal/dev notes.
- **Drop "· N items" from the First-run sweep divider (#50).** The count
  caused confusion ("is the divider counting itself?"). It wasn't —
  `section-header` was always excluded from `victims` — but the count
  added no value on a divider whose swept cards sit immediately below it.
- **Clear stray multi-element text selection on background click (#51).**
  The #659 deselect handler bailed when no single contentEditable was
  focused, so drag-selecting across multiple column headers left a
  persistent blue blob. Now also clears any non-collapsed Range when
  clicking outside both the selection and any editable surface.

### 0.9.27 — Pre-release polish: docs, hygiene, multi-session UX (#563/#658/#659/#610/#385) (2026-06-17)

- **Adoption-focused README + `docs/` entry point + MIT LICENSE (#563).** Rewrote
  the README into a claude-mem-style landing page (problem/fix, hook-enforced
  tracking, prominent install, quick start, configuration, peer comparison).
  Added `docs/README.md` as a clean index so newcomers land on user docs, not
  internal notes. Added the actual MIT `LICENSE` file the badge/footer claimed.
- **`dev/` excluded from the public plugin (#658).** Maintainer-only tooling
  (smoke tests, sims, benchmarks, git-hooks, session loggers — 22 files) was
  shipping to the marketplace install. Gitignored the whole folder and
  `git rm --cached`'d it; files stay on disk locally. No runtime code depended
  on `dev/`.
- **Column-title text-selection no longer sticks blue until you open/close a
  card (#659).** Clicking outside a focused column title now blurs it (committing
  any rename) and clears the stray selection — same root cause as #555, capture-
  phase `mousedown` so the drag handlers can't swallow it.
- **Removed the false-firing "Board changed in another session" undo
  popup (#610).** The guard probed `/rev` and confirmed on ANY rev delta, but
  this agent's own `card.py` writes bump rev constantly — so it false-fired on
  nearly every Cmd/Ctrl+Z (worse with multiple `?sid` sessions). The board is
  last-writer-wins by design (#609) and undo is reversible via the redo stack,
  so the guard was belt-and-suspenders; auto-accept to kill the popup.
- **`_bootstrapFillSeen` no longer sticks past its run (#385).** Was only reset
  inside the `phase==='reconcile' && _bootstrapFillSeen` branch, so any fill
  whose final wasn't a bootstrap-reconcile left the flag set → a later
  standalone live recon was misclassified as a bootstrap and fired a spurious
  full-board `flipResort`. Now capture-then-clear on EVERY `final`. (Residual:
  an abnormally-bailed reconcile that emits zero `final` still needs the
  backend `after_fill` signal — tracked as #662.)

### 0.9.26 — Discarded fixes + collapsible Done-by-day index (#650/#651/#652/#653) (2026-06-17)

- **Discarded cards no longer vanish on hard refresh (#650).** The Discarded column
  isn't persisted in `board.json` (it's lazily created on the first soft-delete), but
  the bootstrap declutter adds `column:'discarded'` cards server-side without creating
  it — so on reload every discarded card (incl. the 🧹 sweep header) had no column to
  render into and disappeared until a manual soft-delete recreated it.
  `ensureDiscardColumnIfNeeded()` now materializes the column on every load.
- **Redesigned the 🧹 first-run sweep divider (#651).** Was one flat emoji string in an
  undefined `--fg` (near-invisible on the light column). Now a structured, quiet
  divider: inline SVG broom (no emoji), real `--muted`/`--ink` tokens, a tabular count
  pill, a hairline rule, and a short date.
- **Collapsible Done-by-day groups + collapse-all dated index (#652).** Click a date
  header to fold/unfold a day; a ⊟/⊞ toggle collapses all days so Done becomes a compact
  dated index for at-a-glance wayfinding + click-to-jump (expands + scrolls into view).
  Pure render-time filter — never touches card data, so sort/fly are unaffected.
- **Flying/dropping a card into a collapsed Done day now lands it under its date header
  and opens the day (#653)**, instead of dumping it at the bottom outside its group.

### 0.9.25 — First-run declutter: faster, HUD-correct, settles in place (#155/#156/#157) (2026-06-17)

- **Declutter glides at 45ms/card (#155)** instead of the inherited 400ms `--pause-ms`
  default + a 250ms loop dwell (~0.65s/card). A `_DECLUTTER_PACE_MS=45` constant drives
  the single fly call; the separate loop sleep is gone. A 12-item sweep's dwell drops
  from ~7.8s to ~0.5s.
- **The bootstrap HUD stays up until declutter finishes, and its count tallies both
  phases (#156).** Previously `reconcile_sweep` emitted the `final` HUD event (→ ✓ COMPLETE
  + auto-hide) before `declutter_sweep` ran, so the HUD vanished mid-sweep and the count
  was reconcile-only. `reconcile_sweep` gained `final_hud` (bootstrap passes `False`);
  declutter shows a "tidying N…" line; and `run()` emits the single combined final —
  "✓ (reconcile + declutter) card(s) brought up to date" — once, after declutter.
- **The 🧹 First-run sweep header settles above its swept cards live (#157).** During the
  one-by-one declutter the header sinks as cards glide in on top of it (only a refresh
  fixed it). The end-of-bootstrap `flipResort` now runs FIRST so the header glides into
  place, then the ✓ COMPLETE HUD shows once the sort settles — so the final HUD is the
  genuine last step.

### 0.9.24 — Drop the HUD "still working" tick + dedupe bootstrap harvest (#121) (2026-06-16)

- **Removed the "still working… mm:ss" HUD tick (reverts #638).** The BOARD-SYNC HUD
  appended a ticking timer to its sub-line whenever a stage went quiet; the running
  clock read as noise. `progress_heartbeat()` is now a no-op context manager (no
  daemon thread, no emit) and `_HudPulse.touch()` is inert, so its two callers need no
  changes. The real per-chunk `N/M` count and "N card(s) emitted so far" sub-line are
  untouched.
- **Bootstrap parses each transcript window once (#121).** `harvest_jsonl` re-reads
  every transcript line regardless of window, so `_flatten_events` cost ~6s/call and
  `run()` paid it up to 3× — tier-1, tier-2, and the end-of-replay reconcile, where
  reconcile re-parsed the *identical* window tier-2 had just read (the "long gap before
  reconciliation"). A `run()`-scoped harvest memo (`@_bootstrap_harvest_cache`) reuses
  an exact `(project, days, sources)` parse and is inert outside `run()` so the
  long-lived server never serves stale events to a later SessionStart recon. Measured
  on real transcripts (8-day window): the harvest pattern dropped 13.24s → 6.25s (~7s),
  with a byte-identical 12954-event result set.

### 0.9.23 — Declutter flies cards in paced (2026-06-16)

- **First-run declutter now glides cards into Discarded one at a time** instead of a
  single batch write that made all N cards teleport at once (looked messy). Each
  victim flies via `card.py fly … --via declutter` at the default glide pace with a
  short dwell, matching the rest of the board's motion. Added `declutter` to the
  `--via` choices + `VIA_LABEL` so the Logs HUD shows "(Declutter) MOVE".

### 0.9.22 — Duplicate-tab fix + first-run declutter (2026-06-16)

- **Duplicate board tabs, eliminated (#122, #150).** `board_autoopen.sh` now serializes
  concurrent opens with a per-port atomic lock + cooldown stamp (kills the simultaneous
  "7 tabs at once" burst) and detects an already-open tab durably: `serve.py` exposes
  `lastSseConnectMs`/`nowMs` in `/health` and the opener treats a tab as present if an SSE
  client connected within the last 20s — robust to the keepalive flap that used to read
  `sseClients` as 0 and spawn a spurious tab. Root flap fixed too: the SSE keepalive write in
  `serve.py._handle_sse` is now wrapped in try/except (was crashing the handler on dropped
  sockets, flooding the log and dropping the client count).
- **First-run declutter sweep (#630).** A fresh bootstrap can mint 100+ low-signal `discovered`
  cards. A deterministic (no-LLM) end-of-replay pass moves cards that are `discovered` AND have
  no work-type tag (`bug`/`feature`/`refactor`/`enhancement`) AND aren't Done to Discarded under
  a dated, reversible `🧹 First-run sweep · <date> · N items` header, so a new user lands on a calm
  board. Runs once at bootstrap, never on recurring recon.

### 0.9.21 — Per-session pulse + multi-session lost-update fix + adoption README (2026-06-10)
- **Per-session active-work pulse (#608, `a6471a9`).** `activeWorkId` scalar → `activeWork`
  map `{sessionId:{cardId,ts}}` so N concurrent sessions show N pulsing cards (max one per
  session). Agent claims via `CLAUDE_CODE_SESSION_ID`; the browser never claims (adopts the
  authoritative map from `rev-bumped`); self-heal + 12h TTL; legacy scalar migrates.
- **Rev-as-CAS lost-update fix (#609, `f6b232a`).** Closes silent agent-vs-agent card loss:
  `serve.py` enforces compare-and-swap on agent POSTs via `X-Board-Base-Rev` (re-checking the
  on-disk rev so a drifted cache can't 409-storm a stuck board); `card.py` reloads + retries
  (12× jittered backoff) on conflict instead of clobbering. Browser writes stay
  last-writer-wins by design. Verified live: 12 parallel adds all land, stale write → 409.
- **Adoption-focused README (#607/#563, `6124b98`).** Rewrote `README.md` as a benefit-first
  pitch (claude-mem style); relocated dev/repo content to `docs/DEVELOPMENT.md`.
- **Marketplace sync.** `marketplace.json` was stale at 0.9.13; bumped it and `plugin.json`
  to 0.9.21. (Note: CHANGELOG entries for 0.9.15–0.9.20 were never backfilled — those bumps
  live in commit messages only.)

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
- **Enforce phase-card model (#107, `3bffe57`).** A fresh-install test caught an agent
  planning a phased project as 18 one-card-per-deliverable in Backlog. Adopted Option A +
  graduation: SKILL.md shape 4 = **1 card per phase** (tagged `phase`, `Phase N — <goal>`)
  in Task, deliverables as subtasks; phase cards never enter `inprogress` — the active
  deliverable **graduates** into its own `--link`'d card. `card.py fly` blocks a phase card
  from entering IP (hands the graduate command); `phase` is a structural tag bypassing
  taxonomy. Pure-A (no graduation) kept as fallback (#110).
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
