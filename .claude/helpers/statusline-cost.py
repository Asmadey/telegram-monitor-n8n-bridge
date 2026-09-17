#!/usr/bin/env python3
"""Status line: model | context % | session cost.

Prefers the figures Claude Code passes on stdin and falls back to computing them
from the session transcript, so it still reports something useful if the shape
of the status-line payload changes.
"""
import json
import os
import sys

# USD per million tokens. Cache writes use the 1h multiplier (2x) when the
# transcript shows 1h-TTL writes, else the 5m multiplier (1.25x).
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
WINDOWS = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-fable-5-1": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
DEFAULT_WINDOW = 200_000


def dig(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def scan(transcript):
    """Last context size and total cost, computed from the transcript."""
    latest, per_msg = {}, {}
    try:
        with open(transcript, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if usage:
                    per_msg[msg.get("id")] = usage   # last record per id wins
                    latest = usage
    except OSError:
        return None, None, None

    model = None
    ctx = (latest.get("cache_read_input_tokens", 0)
           + latest.get("cache_creation_input_tokens", 0)
           + latest.get("input_tokens", 0)) if latest else 0

    cc = cr = inp = out = 0
    ttl_1h = False
    for usage in per_msg.values():
        cc += usage.get("cache_creation_input_tokens", 0)
        cr += usage.get("cache_read_input_tokens", 0)
        inp += usage.get("input_tokens", 0)
        out += usage.get("output_tokens", 0)
        if dig(usage, "cache_creation", "ephemeral_1h_input_tokens"):
            ttl_1h = True
    return ctx, (cc, cr, inp, out, ttl_1h), model


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    model_id = dig(data, "model", "id") or ""
    label = dig(data, "model", "display_name") or model_id or "model?"
    transcript = data.get("transcript_path") or ""

    ctx = dig(data, "context", "used_tokens") or dig(data, "usage", "context_tokens")
    cost = dig(data, "cost", "total_cost_usd")

    totals = None
    if transcript and (ctx is None or cost is None):
        ctx2, totals, _ = scan(transcript)
        if ctx is None:
            ctx = ctx2

    if cost is None and totals:
        cc, cr, inp, out, ttl_1h = totals
        base_in, base_out = PRICES.get(model_id, PRICES["claude-opus-5"])
        write_mult = 2.0 if ttl_1h else 1.25
        cost = (cc / 1e6 * base_in * write_mult + cr / 1e6 * base_in * 0.1
                + inp / 1e6 * base_in + out / 1e6 * base_out)

    window = WINDOWS.get(model_id, DEFAULT_WINDOW)
    try:
        window = int(os.environ.get("CLAUDE_STATUSLINE_WINDOW", window))
    except ValueError:
        pass
    if ctx and ctx > window:          # long-context variant in play
        window = 1_000_000

    parts = [label]
    if ctx:
        pct = 100.0 * ctx / window
        parts.append(f"ctx {pct:.0f}% ({ctx // 1000}k/{window // 1000}k)")
    if cost is not None:
        parts.append(f"${cost:.2f}")
    print(" | ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
