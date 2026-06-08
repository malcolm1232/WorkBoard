# Reinstall-Verification Batch Plan — handoff from session 2026-06-08

**Purpose:** triaged the board's In-Progress + Super-Urgent backlog to separate
*already-done* from *real outstanding work*, grouped the real work into batches,
and defined the reinstall-verification workflow to validate each batch.

**Open this at the START of next session.** It survives `/clean-slate` (it's in
the repo `docs/`, not in `~/.board-steward/` or the plugin cache).

---

## ⏩ PROGRESS — session 2026-06-09 (read FIRST)

**Done & committed (current HEAD has all of these):**
- **Batch 1** ✅ — #524 recon-prose guard + #321 drop-asst-prose (`7c4eed4`). Live-verified autonomous recon emits JSON.
- **Batch 2** ✅ — #378 de-sprawl `~/.agents` paths → telemetry to `~/.board-steward/`, SKILL `<card_py>`, docs (`1d9d190`).
- **#526** ✅ — SSE self-heal watchdog (`cc9b1d4`).
- **#530** ✅ — portable `_BROAD_ROOTS` (`Path.home().parent`, was hardcoded macOS `/Users`) (`545a7ce`). From a portability `/code-review` of the discover2 chain — discovery otherwise confirmed portable across any user/home/path.

**Fresh-install baseline:** clean-slate + marketplace reinstall done this session — picker→bootstrap→55 cards flew (replay)→reconcile moved 12, on fresh code. ("speedup" tier didn't fire = expected: all activity in the recent replay day, older tier empty.) **Real 374-card board restored** from the clean-slate backup; live at :7891.

**OPEN follow-ups (not yet done):**
- **#527** — flag-hygiene: strip redundant `--pause-ms` overrides (`hourly_reconcile.py:207-208`, `BOOTSTRAP.md`, etc.); also the rule "never pass `--pause-ms`, default 400" (saved to memory).
- **#529** — decide: retire the legacy `~/.agents` skill-dir sync (`dev/sync_skill.sh` + post-commit hook), superseded by the marketplace plugin.
- Secondary discover2 review notes (low priority, different axis): convo-dir `HH:MM`/`git --since` timezone handling; `files_from_tool_use` hardcoded 5-tool list (future Claude-Code file-tools won't register).

**NEXT: Batch 3** (archive scheduling + 60-day gate — see below). Per the loop, after Batch 3: clean-slate + reinstall to verify.

---

## 0. The workflow you defined

> Verify the CURRENT commit works on a truly fresh install FIRST. Then solve one
> batch at a time; after each batch, clean-slate + reinstall again to confirm the
> fix holds end-to-end on a fresh user.

**Loop:**
1. **Preserve** the current `board/board.json` (clean-slate auto-backs up to
   `kept-boards/`, but confirm the backup before wiping).
2. `/clean-slate` → uninstall plugin → **reinstall from current commit**.
3. **Verify baseline** (the fresh-user E2E — see §4 checklist). If it works, the
   current commit is a good baseline.
4. Solve **Batch N** → commit → `/clean-slate` + reinstall → verify Batch N fix
   end-to-end.
5. Repeat until all batches done.

Tip: the `/e2e` skill isolates state and asserts the live board is untouched —
use it for regression checks between batches without polluting the real board.

---

## 1. Baseline verification checklist (§4 — "does the current commit work?")

On a clean reinstall, confirm the fresh-user path:
- [ ] First-run picker fires (no `~/.board-steward/.onboarded`) — offers to stand up a board.
- [ ] Pick a project → `serve.py --bootstrap` mines history → cards **fly in** (History Replay), one-by-one.
- [ ] Board opens at `http://127.0.0.1:7891` automatically; `.onboarded` written.
- [ ] Reopen session → **no replay** (idempotent open), digest only.
- [ ] Live carding works: `card.py add → fly inprogress → done` during real work.
- [ ] Stop-hook writes `board/recon_pending.json` at session end (CLAUDECODE=1 path).
- [ ] SessionStart digest surfaces the board + any recon-pending.
- [ ] Multi-board routing: a 2nd project gets its own sticky port (registry), no cross-board mixing.

---

## 2. THE BATCHES (real outstanding work, in suggested order)

Each batch = solve → reinstall → verify. Ordered by "fixes the most, lowest risk" first.

### BATCH 1 — Strip model prose (recon + extraction)  ✅ DONE 2026-06-09 (commit 7c4eed4)
> #524 + #321 both shipped & carded done (board rev 3476). #524 fix = ported the two
> anti-prose guards from _LLM_PROMPT into _RECON_PROMPT; LIVE-verified on the autonomous
> Haiku path (CLAUDECODE unset, question-laden log) → returns JSON + moves cards, no prose.
> #321 fix = build_digest drops asst prose head, keeps "CLAUDE edited:" file line, gated
> DIGEST_DROP_ASST (default on). Smoke 14/14. STILL PENDING: full clean-slate + reinstall
> baseline E2E (§1 checklist) to confirm on a truly fresh install.

- **#524 RECON-PROSE-NOJSON** *(bug, Task)* — autonomous Haiku recon returns
  conversational prose instead of a JSON array → `parse_card_array` finds nothing
  → **0 moves (recon backstop silently no-ops)**. Caught during #229 smoke. Only
  hits the autonomous (CLAUDECODE-unset / launchd) path. **Single run — confirm
  flaky vs systemic first.** Fix: strip preamble before the `[` in
  `parse_card_array`, or force output-format / tighten recon prompt.
- **#321** *(SU)* — Extractor: drop assistant-turn prose from chunk digests.
  CURRENT: `build_digest` (hourly_common.py:84) only **truncates** asst turns to
  the head; the card wants to **drop** them (asst prose = 219K tok ≈ redundant w/
  git). Partial — residual optimization remains.
- **Why together:** both are "model prose contaminating a structured path."
- **Verify after:** autonomous recon actually moves cards on a fresh install
  (the exact condition that surfaced #524).

### BATCH 2 — Plugin path de-sprawl (distribution-readiness)  ✅ DONE 2026-06-09 (commit 1d9d190)
> #378 shipped & carded done (board rev 3496). Telemetry → fixed home dir
> ~/.board-steward/telemetry/ (BOARD_TELEMETRY_FILE override); SKILL.md → <card_py>
> placeholder; docs → plugin-cache path. board.html already clean. Telemetry round-trip
> verified + smoke 14/14. NEW follow-up #529: retire-or-keep the legacy dev/sync_skill.sh
> + post-commit hook that still mirror to the dead ~/.agents skill-dir.

- **#378 DE-SPRAWL-PATHS** *(Before Deploy)* — 6 spots still hardcode the old
  install path `~/.agents/skills/board-steward/` (telemetry + SKILL.md command
  examples + README + board.html error msg). On a distributed plugin (installed
  under `~/.claude/plugins/cache/workboard/board-steward/<ver>/`) these break:
  telemetry writes to a stale dir; SKILL.md tells Claude to run `card.py` from a
  path a fresh user won't have. Hooks already use the portable
  `${CLAUDE_PLUGIN_ROOT}` — extend that consistency.
  - Exact spots: `scripts/hook_user_prompt.sh:69`, `scripts/log_event.py:2,44`,
    `scripts/report.py:19`, `SKILL.md:164,185`, `README.md:40`,
    `templates/board.html:6294`.
  - **Decision needed:** telemetry events file — keep at a fixed home dir
    (stable across plugin upgrades, like `~/.board-steward/`) vs under plugin
    root. Recommend a FIXED home dir (data shouldn't live in a versioned cache
    that's wiped on upgrade); only the *script/SKILL* refs must become portable.
- **Note:** #278(B) "de-hardcode harvest paths" = this work (#278 itself is now
  CLOSED — its 4 children #279/#280/#286/#281/#287/#282 all Done).
- **Verify after:** reinstall → telemetry writes to the right place; SKILL.md
  `card.py` path resolves; no stale `~/.agents` refs.

### BATCH 3 — Archive scheduling + 60-day gate (board bloat)
- **#203 BOARD-ARCHIVE-SCHED** *(IP)* — `archive_done.py` exists but is **never
  auto-invoked** in production (only by `dev/sim_60d.py`). LIVE EVIDENCE today:
  `index.json` = **187 KB** (target ≤48 KB, ~4× over); `board.json` = **702 KB**;
  324 Done cards never swept; no `board/archive/` dir. Browser polls `/board.json`
  every 3s → real perf cost.
- **#87 SIM-60D** *(IP, coupled — linked)* — the 60-day late-adopter sim;
  `--strict` currently **FAILs on the "archive-on-install gap"**. It's the gate
  that proves the #203 fix works at scale.
- **The coupling subtlety:** scheduling alone won't fix a fresh install —
  bootstrap stamps mined cards with ~NOW `doneAt`, so the 14-day sweep won't
  touch them (`sim_60d.py:162`). Need **#87(c): bootstrap assigns historic
  doneAt** so the sweep applies immediately.
- **Work:** (1) register daily `archive_done.py --days 14` in `install_launchd.py`
  / `install_systemd.py` / `install_taskscheduler.py`; (2) historic-doneAt on
  bootstrap; (3) wire `sim_60d --strict` as the launch-gate + decide strict-cap
  policy.
- **Relieves:** #113 BOARD-LAZY-RENDER (the bloat that looks like it's nearing
  #113's 500-card trigger is mostly un-swept Done cards — #203 removes it).
- **Verify after:** reinstall + archive sweep → `index.json` ≤48 KB;
  `sim_60d --strict` passes.

### BATCH 4 — Extraction source-selection + engine choice
- **#322** *(SU)* — Source-selection by availability (git > convo > jsonl). Not
  implemented (`SOURCES` param exists, no availability-ranked fallback chain).
  Follow-on to #321.
- **#277** *(SU)* — Run a 60-day sim **haiku vs inline** and compare; you suspect
  inline is "already so much faster" even at 1 day and haiku may be unnecessary.
  Decision-experiment; couples with #87's sim harness.
- **#264 BOARD-DEMO-LIGHT** *(SU)* — Rework demo fly to be compute-light (current
  demo fires one `claude -p` Haiku call per bucket). You explicitly deferred this
  ("later we'll make it compute-light").
- **Verify after:** extraction picks the best available source per project;
  engine decision recorded.

### BATCH 5 — SKILL.md slimming / phase-conditional loading + token cost
- **#202 BOARD-SKILL-SLIM** *(IP)* — SKILL.md is **~5,493 tok** (284 lines) vs the
  ≤2,000-tok target. BOOTSTRAP.md already split out; PLAYBOOK/SCHEMA/TAGS/INSTALL
  splits NOT done.
  - **YOUR REFINED APPROACH (recorded on the card):** phase-conditional loading
    instead of a flat trim. Gate on the existing `~/.board-steward/.onboarded`
    marker: FIRST install → read `docs/BOOTSTRAP.md`; AFTER bootstrap → SKILL.md =
    lean LIVE-protocol SPINE only (laws + lifecycle E + reconciliation F + carding
    LAW + decision table), skip bootstrap content every engagement.
  - **GOALS:** fewer tokens on the common path + **tighter law-adherence** (less
    noise → carding discipline more prominent).
  - **CRITICAL VALIDATION:** must NOT change how live cards are carded — the LAWS
    + lifecycle stay fully intact in the post-bootstrap spine. (This is why you
    deferred it — fear of affecting live carding.)
- **#478** *(SU)* — Live-test each carding/subtask possibility; **contains the SAME
  phase-conditional SKILL.md idea** ("after BOOTSTRAP, delete part 1 of SKILL.md
  so it's not diluted") + "run the stop hook once." → merge with #202.
- **#296** *(SU)* — "Will WB eat free users' tokens?" — answered by the slimming +
  token-budget work. Resolve as the verified answer.
- **Verify after:** reinstall → first run loads bootstrap guidance; subsequent
  sessions load the lean spine; **live carding behaviour identical** to baseline.

### BATCH 6 — Regression / live-test harness (release confidence)
- **#316** *(SU)* — Lock today's smoke (import + 22 commands + static audit) as a
  permanent regression harness wired to the post-commit hook; run haiku/discover
  live once to exhaust cold-path NameError risk; ship #315 (never-miss guarantee);
  clear launch-blocking cards → release.
- **#478** *(SU)* — live-test each carding/subtask (overlaps Batch 5).
- **#87 SIM-60D** — the strict sim is part of this gate (overlaps Batch 3).
- **Verify after:** `make test` / harness is one-command green; post-commit gate active.

---

## 3. CONVERGENCES — don't do the same work twice

Several Super-Urgent cards are the SAME work as non-SU cards, filed from a
different angle. Merge/keep-linked:

| Super Urgent | = | Non-SU card | Theme |
|---|---|---|---|
| #321 | ≈ | #524 | strip model prose |
| #278(B) | ≈ | #378 | de-hardcode plugin/harvest paths |
| #478 / #296 | ≈ | #202 | phase-conditional SKILL + token cost |
| #277 / #316 | ≈ | #87 | sim / regression gate |

---

## 4. CLOSED THIS SESSION (for the record — do not redo)

**In-Progress sweep (already done / superseded / obsolete):**
- #229 SNAPSHOT-LOAD-SMOKE — smoke-verified (isolated).
- #226 HOURLY-RETRY-TIMEOUT — superseded by recursive sub-bucketing (smoke-proven
  on the 05-27 chunk it historically lost).
- #228 INLINE-RECON-SMOKE — full write→surface→action→delete loop smoke-verified.
- #227 HOURLY-BANNER-BUCKET-TS — obsolete (banner card removed 6/1, #318 HUD).
- #212 TEST live-board quality — already verified (writeup existed).
- #204 BOARD-HOOK-MULTIPORT — fixed via #374 sticky port registry.

**Super-Urgent sweep (closed):**
- #134 column-reorder v2 — shipped c256bd7 then REVERTED (you preferred baseline).
- #278 LIVE-BACKSTOP — superseded; 4 children all Done.
- #289 HARVEST-SUBAGENT-TX — investigated; `~/.claude/tasks` + `sessions` empty,
  no harvest gap.

**Filed this session:** #524 RECON-PROSE-NOJSON (bug).

---

## 5. OPEN DECISIONS for you (quick calls, pre-batch)

- **#267** demo cross-project leak — silent-leak BUG is fixed (scoping +
  `_event_in_project`, #508). But bootstrap **deliberately** seeds cross-project on
  an empty repo (#285, "so day-one isn't blank"). DECIDE: close as
  superseded-by-#285, OR keep open as a **seed-QUALITY** follow-up (the "plop
  slices" complaint — filter/curate seeded cards).
- **#262 BOARD-LOG-PERSIST** — recommend **discard**: traceability already lives in
  on-disk `.board-server.log` / `.board-recon.log`; the card is just a UI
  convenience and adds a per-SSE-event disk write (against keep-webapp-lightweight).
- **#113 BOARD-LAZY-RENDER** — speculative 500+ scale; #203 relieves the pressure.
  Move back to **Backlog** (it drifted into IP; notes say it was demoted 5/31).
- **#230 SHIMMER-SAFARI** — trivial: 2-min Safari look during a live extractor run,
  or **discard** (low value for a localhost tool).
- **#128** chess-grid drag layout — UI experiment vs the fragile reflow drag. Build
  or park.

---

## 6. STATE CAVEATS

- **Preserve board.json before clean-slate.** This session closed ~15 cards;
  clean-slate backs up to `kept-boards/` but confirm it.
- **Live `recon_pending.json`** (written 07:02) listed the 9 IP cards we triaged —
  it can be deleted to close the loop (regenerates each session-end until IP is
  actually clean).
- **Bloat is live** (#203): index.json 187 KB, board.json 702 KB — the archive
  batch reclaims this.
- Board rev at handoff: ~3472. ~373 cards (324 Done).
