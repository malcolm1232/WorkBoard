"""Shared tokenizer used for every count in the 2026-06 study.

CRITICAL FAIRNESS CONTROL: both WorkBoard and claude-mem (and any other peer)
are measured with the same function below. Do not import a different
tokenizer elsewhere in dev/bench/.

Default backend: tiktoken cl100k_base.
  - Deterministic, offline, no API key.
  - Documented to be ~10-15% lower than Claude's true tokenizer.
  - It is the tokenizer claude-mem uses internally for their own claims, so
    measuring both systems with it favors neither.

Opt-in: set BENCH_TOKENIZER=anthropic AND ANTHROPIC_API_KEY to use the
official Anthropic count_tokens API (requires network).
"""

from __future__ import annotations
import functools
import os

_BACKEND_NAME = None


@functools.lru_cache(maxsize=1)
def _backend():
    global _BACKEND_NAME

    if os.environ.get("BENCH_TOKENIZER") == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()
            model = os.environ.get("BENCH_TOKENIZER_MODEL", "claude-haiku-4-5-20251001")
            _BACKEND_NAME = f"anthropic-count_tokens({model})"

            def _count(text):
                r = client.messages.count_tokens(
                    model=model,
                    messages=[{"role": "user", "content": text}],
                )
                return int(r.input_tokens)
            return _count
        except Exception:
            pass

    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        _BACKEND_NAME = "tiktoken-cl100k_base"
        return lambda text: len(enc.encode(text, disallowed_special=()))
    except Exception:
        pass

    _BACKEND_NAME = "chars-over-4"
    return lambda text: max(1, len(text) // 4)


def count(text):
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    return _backend()(text)


def backend_name():
    _backend()
    return _BACKEND_NAME or "unknown"


if __name__ == "__main__":
    import sys
    sample = sys.stdin.read() if not sys.stdin.isatty() else "hello world"
    print(f"backend: {backend_name()}")
    print(f"tokens : {count(sample)}")
