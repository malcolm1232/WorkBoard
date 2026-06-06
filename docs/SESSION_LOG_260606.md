# Session Log — 2026-06-06

Theme: **codify the multi-part carding LAW, then validate it with a real
fresh-install test.** Records what was done, in order, and what changed per step,
so we know *when we changed what*.

---

## 1. Infrastructure changes shipped (committed)

| Card | What | Why | Commit | Files |
|---|---|---|---|---|
| **#384** | Wrap end-of-replay reconcile in `try/finally` | If `reconcile_sweep` raised, the replay gate stuck at `completed_card_replay=0` → all future SessionStart recon silently skipped that board (regression from 23fdc02) | `26d58fb` | `scripts/hourly_extractor.py` |
| **#474** | Reshaped as a live example of the new law | Old card crammed parts in the title; now title=`Unify urgent column` + 3 subtasks | (board-only) | — |
| **#476** | **Codified the multi-part carding LAW** in SKILL.md | Naming was discretionary → inconsistent across installs. Header test now decides shape | `2d037cf` | `SKILL.md` |
| **#103** | **Enforce decompose-before-IP** (wording + guard) | Test #2 caught the agent flying a multi-part card to IP with 0 subtasks | `71fabac` | `SKILL.md`, `scripts/card.py`, `scripts/card_commands.py` |

### The LAW (from #476, strengthened by #103)
- **Header test:** one honest `verb + noun` header covers all parts → **1 card + N subtasks**; no single header → **N cards**.
- **Decompose BEFORE inprogress** (shapes 2a/3/4): `add` → `subtask add ×N` *(in Task)* → `fly inprogress` → `subtask done` per part → `fly done`. Subtasks must exist before the card flies.
- **Guard:** `card.py fly … inprogress` now blocks a multi-part-looking card (`_looks_multipart`: colon-list / ≥2 commas / numbered / `, and`) that has 0 subtasks, on the task/backlog→IP hop. Override: `--force` or `BOARD_SKIP_DECOMPOSE_CHECK=1`.

---

## 2. Carding-law fresh-install test

- **Plan mode** → wrote `~/.claude/plans/silly-inventing-simon.md` (approved). Key finding: **subagents can't validate the law** (they don't load SKILL.md; default mode collapses their work to one subtask) → the valid vehicle is a **fresh main session**.
- **Phase A — refresh:** pre-backup board (338 cards ×3 copies) → `/clean-slate` (backs up + wipes + nukes plugin cache) → reinstall. Cache then verified to carry the new law.
- **Bootstrap verification:** the fresh board bootstrapped and **all phases fired** (from `.board-server.log`): `replay` (54 cards) → `speedup` (46 cards) → `reconcile` (13 moved). This also **live-validated #384** — the end-of-replay reconcile ran and the gate reopened (`completed_card_replay: 1`).

---

## 3. Test matrix (carding-law scenarios)

| # | Scenario | Prompt | Status | Result / what changed |
|---|---|---|---|---|
| **1** | 1 task → 1 card | "Add a dark-mode toggle to the settings page." | ✅ **PASS** | 1 card, clean title. Test agent built a real dark-mode toggle → `templates/board.html` (+48 lines, **uncommitted** — see §4). |
| **2** | many related → 1 card + subtasks | "Unify the urgent column: route reconcile…, retire mandatory, fix launch-block double-count." | ❌ **FAIL → fixed → re-test pending** | Agent flew #102 to IP with **0 subtasks** (parts lost, showed `1/1`). Fixed by **#103** (ordered procedure + guard). Cache refreshed; awaiting re-run. Test agent's urgent cleanup → 6 scripts (**uncommitted** — see §4). |
| **3** | many unrelated → N cards | "Three separate things: fix auth redirect, add rate-limit header, update README." | ⏳ **IN PROGRESS** (user testing) | — |
| **4** | phases → 1 card per phase | "Create a new workboard for an Edu platform and plan it end-to-end in phases." | ⬜ **NOT STARTED** | — |

---

## 4. ⚠️ Git hygiene — test-generated code is uncommitted (decision needed)

The scenario agents made **real repo edits** that are NOT covered by the board
restore (Phase C only restores `board.json`). Current working tree:

- **Test 1 artifact:** `templates/board.html` (+48 lines, dark-mode toggle) — uncommitted.
- **Test 2 artifact (#102):** `scripts/{hook_session_start.sh, digest_compact.py, discover2_extract.py, hourly_reconcile.py, hourly_extractor.py}`, `skills/e2e/e2e_workboard.py` — uncommitted. *(The `card_commands.py` portion of #102 was inadvertently swept into commit `71fabac` alongside the #103 guard — the two are conflated in that commit.)*
- `.gitignore` (+`board/.replay_state.json`) — a bootstrap side-effect, safe to keep.

**Decision required:** keep these test-generated changes (dark-mode feature + urgent
cleanup) and commit them cleanly under their cards, or revert them as throwaway test output.

---

## 5. Outstanding

- Re-test **Test 2** in a fresh session (cache now has #103).
- Run **Test 3** and **Test 4**.
- Resolve §4 (commit vs revert test-generated code).
- **Phase C** — restore the 338-card board from backup `~/board-steward-cleanslate-backup-20260606-103927/` when testing is done.
