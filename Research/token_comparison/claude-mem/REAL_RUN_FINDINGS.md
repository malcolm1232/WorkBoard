# Real claude-mem run — findings (sandboxed, 2026-06-18)

We attempted the empirical validation from REPRODUCIBILITY.md §5: install and run
claude-mem 13.6.1 **for real**, fully sandboxed, on the frozen `tiny` corpus, to
replace the modeled numbers with measured ones.

**Outcome: the full stack ran; the compression model call was blocked by auth
(the sandbox is intentionally not logged into Claude). We measured the
*structure* for real, which validates the bootstrap model; the per-observation
*output* token sizes remain from claude-mem's published spec.**

## What ran (real, sandboxed)

- Standalone **node 22** (`/tmp`, system node untouched).
- claude-mem `install` into a throwaway `$HOME=/tmp/cm-real-home` → auto-installed
  **Bun 1.3.14** + **uv 0.11.21**, registered the plugin, started the **worker
  service** on port 37701. Confirmed `~/.claude` (real) was never touched.
- Symlinked the **same frozen `tiny` transcripts** WorkBoard used.
- Triggered claude-mem's real compression path — the **Stop hook →
  `worker-service.cjs hook claude-code summarize`** — on a session. It enqueued
  the job, auto-started the **Claude Agent SDK** generator, found the `claude`
  CLI (v2.1.181), and attempted to compress.

## The wall

```
authMethod = Claude Code OAuth token (read from system keychain at spawn)
← SDK response: "Not logged in · Please run /login"
```

claude-mem's worker compresses via the Claude SDK using **subscription/OAuth
auth**. The sandbox HOME is deliberately not logged in, and extracting the
host's login into the sandbox is both unsafe and against the isolation the study
guarantees. So the compression LLM call could not complete. (To finish it, a user
would run `/login` *inside the sandbox* — see "To complete" below.)

## What this DID establish (real, not modeled)

1. **claude-mem ingests exactly one compression call per session**, triggered by
   the Stop/SessionEnd hook, reading that session's transcript. This **validates
   the structure** of Study A's model (`model_calls = sessions`, `input = full
   transcript tokens`) — the basis of the **98.6–99.2% input-token reduction**.
2. **claude-mem has no bulk/bootstrap/backfill command.** Ingestion is strictly
   per-session via live hooks (confirmed against the full CLI surface + worker
   routes). To "bootstrap" N historical sessions you replay each through the
   summarize hook — N compression calls. This is an architectural difference:
   **WorkBoard explicitly mines past history; claude-mem only compresses forward
   from install.**
3. **The compression runs on the main Claude model via subscription**, through
   the Claude Agent SDK — *not* a cheaper detached tier. This **corrects** an
   earlier Study C caveat: claude-mem's per-session write is on the **same model
   tier as interactive work**, so its live memory-write cost is, if anything,
   *more* expensive than first framed — strengthening WorkBoard's "0 model calls"
   advantage, not weakening it.

## What remains MODELED (auth-blocked)

- Per-observation **output** token sizes and the search-index size — still from
  claude-mem's published spec (`get_observations` ~500-1,000 tok/result;
  `search` ~50-100 tok/result). The real run could not emit these without the
  compression call completing.

## Net effect on the study

- **Study A (bootstrap):** structure validated empirically. The headline
  (~99% fewer input tokens, calls = sessions) stands on firmer ground.
- **Study C (live):** corrected — claude-mem's per-session compression is
  full-tier subscription compute, not a cheap detached call. WorkBoard's
  zero-model-call write advantage is reinforced.
- **Recall detail tokens:** unchanged — still claude-mem's own published numbers.

## To complete the measurement (optional, user-gated)

Inside the sandbox HOME, run `claude /login` (or set `ANTHROPIC_API_KEY`) so the
worker can authenticate, then re-trigger the summarize hook over the 14
substantial `tiny` sessions and read the resulting observations from
`~/.claude-mem/claude-mem.db`. Record per-observation token sizes into
`results/raw/claude_mem_tiny_real.json` and re-render. This is the only step that
needs a human auth decision; everything else is automated and reproducible.
