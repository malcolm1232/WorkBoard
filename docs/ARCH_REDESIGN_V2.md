# Architecture Redesign v2 — Multi-board + Reconciliation (2026-06-04)

> **What this file is.** A durable record of the second major board-steward
> re-architecture, so future work can reuse it without re-deriving the design.
> **v1** (≈May 30–31, commits #307/#308) was the *internal cleanliness* redesign:
> split oversized files, killed god-functions, removed repo↔plugin duplication —
> the "godfunc" work. **v2** (this file) is the *capability* redesign: make the
> product support **multiple boards** and keep them **factually correct** via
> reconciliation, then harden for public launch. Verbatim session transcript:
> `~/Desktop/conversation_history/conversation_verbatim_260604.md`.

---

## Why v2 happened

The original product assumed **one board per machine** and relied on an
end-of-session (Stop) hook to keep the board honest. Two problems surfaced as we
moved toward public launch:

1. **Multiple projects need multiple boards.** One human works across several
   repos; each deserves its own living board, correctly routed.
2. **The board drifts from reality.** Cards sit In-Progress long after the work
   shipped; important points get said but never carded. The Stop-hook backstop
   is unreliable (users close the terminal) and was only a cheap heuristic.

Plus launch blockers (personal paths in shipped code, a pre-release version).

---

## Overhaul 1 — Multi-board (one board per project)

**Finding:** the core (`card.py`, `serve.py`, `port_registry.py`, most hooks)
was *already* mostly multi-board via cwd-walk + a sticky port registry. Only
three single-board assumptions remained.

| Fix | File | Commit |
|---|---|---|
| **Last-active pointer** replaces the newest-mtime tie-break (the wrong-board bug at `$HOME`). `port_registry.set_active/get_active` (`~/.board-steward/last-active`); written by `card.py` on mutation + `bootstrap_project.sh` on create; read by `hook_session_start.sh`. | `port_registry.py`, `card.py`, `bootstrap_project.sh`, `hook_session_start.sh` | `a6a8805` |
| **Flash hook resolves the port via the registry**, not a hardcoded `(7891..7895)` tuple (broke on board #6+). | `_hook_flash_linked.py` | `bbd2268` |
| **Server has no hardcoded port default** — `--port` defaults to `None`; `port_registry.assign()` resolves it. Dropped dead `DEFAULT_SERVER_URL`. | `serve.py`, `card_state.py` | `f1a39cb` |

**Key decision (locked with the user):** at `$HOME` with several boards, reopen
the **last-active** board (the one whose cards were last mutated / just
bootstrapped), *not* the newest file mtime — mtime picked the wrong board when
two boards were touched the same session.

**Design principle:** `port_registry.py` is the single owner of all cross-board
state in `~/.board-steward/`: liveness registry + sticky port-assignments +
last-active pointer. Multi-board ≠ multi-user (still one human, no accounts).

---

## Launch hardening (same session)

| Fix | File | Commit |
|---|---|---|
| Picker example genericized (was a personal path leaking to end users) | `hook_session_start.sh` | `6b4ea1c` |
| Version `0.9.5 → 0.9.6` (1.0.0 deferred to a dedicated release pass) | `.claude-plugin/plugin.json`, `marketplace.json` | `6b4ea1c` |
| `measure_digest.py` portable (`$HOME`/`--project`/env, not a hardcoded home) | `measure_digest.py` | `6b4ea1c` |
| `install.sh` `cp -R` fallback when `ln -sfn` can't symlink (Windows) | `install.sh` | `6b4ea1c` |

Validated by a `/clean-slate` wipe → reinstall → first-run picker rehearsal
(picker fired, personal-path scrub confirmed live), then the real 53-card board
restored from backup.

---

## Overhaul 2 — Reconciliation

**The objective (the durable definition):** keep the board a *faithful mirror of
reality*. Four moves: (1) **In-Progress → Done** when activity shows it shipped
(the most common drift); (2) **surface MANDATORY/IMPORTANT** said-but-not-carded
→ `mandatory` (the STAR — never miss an important point); (3) **capture
un-carded work**; (4) **demote abandoned** (skip/nvm → backlog; stale → backlog).

**The two-layer model:**

- **Stop hook** (`_hook_stop_recon.py`) — cheap, no-LLM, *live* per-turn
  guarantee that net-new work gets carded in the moment (blocking "card it
  now"). *Unchanged in v2.*
- **Smart reconciliation** (`hourly_reconcile.reconcile_sweep`, Haiku) — reads
  recent activity and applies moves 1/2/4, animating cards on the live board.
  Now runs at **two triggers**: **Bootstrap** (post-extraction, pre-existing)
  and **SessionStart** (new).

**Why SessionStart + Bootstrap (not Stop/mid-session):** end-of-session is
unreliable (users close the terminal); mid-session is too costly. SessionStart
catches the *previous* session's drift before the user starts the next one.

**Why Haiku + gates:** Haiku (via the user's `claude` login, no API key) keeps
cost low — free users have minimal tokens. SessionStart recon is gated so it
never burns a call needlessly.

| Change | File | Commit |
|---|---|---|
| `reconcile_sweep(only_discovered=True)` — `True` keeps the bootstrap "discovered-tag only" scope; SessionStart passes `False` to reconcile **live** (untagged) cards too. | `hourly_reconcile.py` | `5b5c320` |
| `--reconcile-only` mode: gated (Gate A: non-done cards exist; Gate B: project activity since last recon via `_last_activity_ms`/history.jsonl), since-last-recon window (first run 2d, cap 14d), project-scoped harvest (`_filter_events`), marker `board/.recon_state.json`. Refactored `_anchor_offset_days` to share `_last_activity_ms`. | `hourly_extractor.py` | `91ed0b9` |
| SessionStart spawns it **detached** with `env -u CLAUDECODE` (forces the Haiku path, not the in-session prose `recon_pending` path). `BOARD_NO_RECON=1` opt-out. | `hook_session_start.sh` | `13716e1` |
| Bootstrap fill+recon also run Haiku — `env -u CLAUDECODE` on the two `serve.py --bootstrap` spawns. | `hook_session_start.sh`, `bootstrap_project.sh` | `1212b0f` |
| Skip SessionStart recon on the bootstrap turn (it was racing the still-streaming fill → lost-update on the full-replace POST). | `hook_session_start.sh` | `0cc5ce9` |
| **SessionStart reconcile REMOVED (#800).** The rows above describe the original design; SessionStart no longer reconciles. The hook now spawns `--backfill-only` (dropped-card recovery only, `_backfill_failed_buckets`), not `--reconcile-only`. The `--reconcile-only` mode + its gates/`.recon_state.json` (rows 97‑98) survive for manual/test use; only the startup wiring changed. Bootstrap recon (rows 96, 99‑100) is unaffected. | `hook_session_start.sh`, `hourly_extractor.py` | working tree |

**Two load-bearing gotchas (documented so v3 doesn't reintroduce them):**
1. `reconcile_sweep`'s candidate filter required the `"discovered"` tag — that
   tag is only on bootstrap-mined cards, so live In-Progress cards were silently
   skipped. Hence `only_discovered=False` for SessionStart.
2. `reconcile_sweep` writes a prose `recon_pending.json` when `CLAUDECODE=1`. A
   hook-spawned process *inherits* `CLAUDECODE=1`, so every autonomous-Haiku
   spawn must `env -u CLAUDECODE`.

---

## How it was verified — the reusable `/e2e` harness

All of v2 is regression-tested by the **`/e2e` skill**
(`~/.claude/skills/e2e/`, see its `SKILL.md`). Run:

```bash
python3 ~/.claude/skills/e2e/e2e_workboard.py all          # multiboard + recon (free)
python3 ~/.claude/skills/e2e/e2e_workboard.py all --haiku  # + real Haiku recon E2E
```

Cardinal rule baked in: **isolated state + throwaway boards + assert the live
board is untouched**. Proven results this session: 10/10 free-tier + 2/2
Haiku-tier passing, live board unchanged. Each overhaul was also reviewed by an
adversarial code-review agent before commit (multi-board: 1 should-fix found+fixed;
recon: 1 should-fix found+fixed — the bootstrap-race).

**Future overhauls reuse this harness** — add `test_<group>_<name>(ctx)` and
register it in `GROUPS`. Don't hand-roll throwaway test scripts again.

---

## Pending (tracked, not done)

- **Recon v2 — extract at SessionStart** (board card **#62 RECON-V2**, backlog).
  Today SessionStart recon is reconcile-*only* (moves existing cards). The v2
  adds a scoped *extract* pass over the since-last-recon gap to **create** cards
  for important work that was never carded — closing the net-new gap the Stop
  backstop only partially covers. Design risk to resolve first: dedup against
  existing cards.
- **Cut 1.0.0** — after a clean multi-board rehearsal (currently 0.9.6).
- **3 minor nesting refactors** (`cmd_update`, `cmd_auto_ship`, `_extract_haiku`)
  — cosmetic, deferred from v1's audit.

---

## Session narrative (what was asked → what was done)

1. **"Support multiple WorkBoards + launch the plugin."** → mapped the codebase
   with 3 Explore agents (multi-board routing / launch readiness / architecture
   audit). Audit verdict: v1 cleanliness held (33 modules, no cycles). Found 3
   real multi-board bugs + 2 launch blockers. Shipped Overhaul 1 + launch
   hardening; rewrote VISION.md for the multi-board workflow.
2. **"Push + run the destructive `/clean-slate` rehearsal."** → pushed 0.9.6;
   wiped + reinstalled + verified the first-run picker fires with the path-scrub
   live; restored the real board from backup.
3. **Reconciliation design discussion** ("what's the objective? run it at
   SessionStart?"). → clarified the 4-move objective + two-layer model; shipped
   Overhaul 2 (recon at SessionStart + Bootstrap, Haiku, gated).
4. **"Save the deferred extract-v2."** → backlog card #62.
5. **"Write an `/e2e` skill so we don't rewrite tests; record this as redesign
   v2; feature it in VISION.md; dump the verbatim convo."** → this file, the
   `/e2e` skill, the VISION.md reference, and the verbatim transcript dump.
