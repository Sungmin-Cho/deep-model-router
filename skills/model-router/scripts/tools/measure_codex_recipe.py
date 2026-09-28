#!/usr/bin/env python3
"""Paired measurement of the codex reviewer recipe (design 2026-09-25 DD-B9,
plan B8).

Two read-only runs of ONE model on ONE fixed review prompt from ONE empty,
throwaway working directory: the current recipe, and the same argv plus
`--ignore-user-config --ephemeral`. What is compared is the boot input the
`codex-exec-json-v1` envelope reports (`turn.completed.usage.input_tokens`),
and whether the review keeps its format (`=== REVIEW ===`, a `verdict:` line).
The candidate is adopted only at a boot-input drop of at least 20% with the
format intact; the tool reports, a person changes the recipe.

Quota first, and without running codex to find out: `model_sync.py quota`'s
reading is taken as it is, and one that defers — at or above 90% used, or
`unknown` (no record, or older than six hours) — ends the tool before any
codex process starts. The gate is not bypassed.

    measure_codex_recipe.py --out-dir <evidence dir> [--model-key openai_reasoning]

Exit 0 whether measured or deferred; 2 on bad arguments.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import dispatch_agent  # noqa: E402
import model_sync  # noqa: E402
import route_task  # noqa: E402

ADOPT_BELOW = 0.80           # candidate boot input <= 80% of the current recipe's
CANDIDATE_FLAGS = ["--ignore-user-config", "--ephemeral"]
PROMPT = """You are reviewing a one-line change. Reply with a line `=== REVIEW ===`, then a
line `verdict: PASS` or `verdict: FAIL`, then at most two sentences.

--- a/add.py
+++ b/add.py
-def add(a, b): return a - b
+def add(a, b): return a + b
"""


def current_argv(codex: str, model_id: str) -> list[str]:
    return [codex, "exec", "-m", model_id, "-c", "model_reasoning_effort=low",
            "-s", "read-only", "--skip-git-repo-check", "--json", "-"]


def run_once(argv: list[str], cwd: str, timeout: int) -> dict:
    proc = subprocess.run(argv, input=PROMPT, capture_output=True, text=True,
                          cwd=cwd, timeout=timeout)
    envelope = dispatch_agent._decode_envelope(proc.stdout.encode(), dispatch_agent.CODEX_JSON_FORMAT)
    text = envelope.get("text") or ""
    lines = [line.strip() for line in text.splitlines()]
    return {"argv": argv, "returncode": proc.returncode,
            "input_tokens": (envelope.get("usage") or {}).get("input_tokens"),
            "usage": envelope.get("usage"),
            "format_ok": "=== REVIEW ===" in lines and any(l.startswith("verdict:") for l in lines),
            "stdout": proc.stdout, "stderr": proc.stderr}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--model-key", default="openai_reasoning")
    p.add_argument("--codex", default="codex")
    p.add_argument("--sessions-dir", type=Path, default=Path.home() / ".codex" / "sessions")
    p.add_argument("--timeout", type=int, default=600)
    args = p.parse_args(argv)
    cfg = route_task.load_config()
    if args.model_key not in cfg["models"] or cfg["models"][args.model_key]["family"] != "openai":
        p.error(f"--model-key must name an openai registry row, got {args.model_key!r}")
    model_id = cfg["models"][args.model_key]["id"]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    quota = model_sync.read_quota(args.sessions_dir)
    if model_sync.quota_defers(quota):
        record = {"measured": False, "at": now, "reason": "quota", "quota": quota,
                  "model_key": args.model_key}
        (args.out_dir / "b8-deferred.json").write_text(json.dumps(record, indent=2) + "\n")
        print(f"deferred: quota {quota.get('status')} "
              f"({quota.get('reason') or 'used ' + str(quota.get('used_percent')) + '%'}); "
              f"no codex process was started")
        return 0

    base = current_argv(args.codex, model_id)
    cwd = tempfile.mkdtemp(prefix="dmr-b8-")        # one cwd for the pair (plan B8)
    try:
        runs = {"current": run_once(base, cwd, args.timeout),
                "candidate": run_once(base[:-1] + CANDIDATE_FLAGS + base[-1:], cwd, args.timeout)}
        leftovers = sorted(os.listdir(cwd))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    for name, run in runs.items():
        (args.out_dir / f"b8-{name}.stdout.jsonl").write_text(run.pop("stdout"))
        (args.out_dir / f"b8-{name}.stderr.txt").write_text(run.pop("stderr"))
    cur, cand = runs["current"]["input_tokens"], runs["candidate"]["input_tokens"]
    ratio = (cand / cur) if cur and cand is not None else None
    adopt = bool(ratio is not None and ratio <= ADOPT_BELOW
                 and runs["current"]["format_ok"] and runs["candidate"]["format_ok"])
    summary = {"measured": True, "at": now, "model_key": args.model_key, "quota": quota,
               "cwd": cwd, "cwd_left_after_runs": leftovers,
               "runs": runs, "ratio": ratio, "adopt": adopt}
    (args.out_dir / "b8-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"current {cur} / candidate {cand} input tokens; ratio {ratio}; adopt {adopt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
