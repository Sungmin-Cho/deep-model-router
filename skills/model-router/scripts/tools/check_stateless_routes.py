#!/usr/bin/env python3
"""Decision-field baseline for stateless routes (plan §0.2, model-lineage tranche).

Routes a FIXED set of 54 requests through the real CLI entry point
(`route_task.main`) with the overlay switched off and an empty state
directory, and keeps only the decision fields of each route. Written once
against a release, the file is the oracle a later change is compared with:

    check_stateless_routes.py --out baseline.json
    check_stateless_routes.py --compare baseline.json [--id-succession id-succession.json]

The request set is enumerated here, not chosen by whoever runs the tool, so
two runs by two implementers compare the same routes:

* IMPLEMENTATION x (c,u,b,r) in {LOW (0,0,0,0), MEDIUM (2,1,1,1),
  HIGH (2,2,1,1), CRITICAL (2,2,2,2)} x runtime {claude_code, codex, grok}
  x flags {none, security_sensitive, production_hotfix, bridge_down} = 48
* REVIEW (2,2,2,1) on claude_code, without and with a complete
  `review_context` whose author is the `claude_senior` registry row = 2
* DEBUGGING (1,2,1,1) on claude_code and codex, history-free and then with
  one prior failure on the history-free route's selected model = 4

Each route's `request` is compared as well as its decision (the DEBUGGING
prior id is part of the request). Ignored: `policy_sha256`,
`decision_fingerprint` (neither is extracted) and a `model_overlay` of null
(only a non-null overlay is kept as a decision field). `--id-succession`
rewrites every superseded id of a chain to the chain's last id on the
BASELINE side only, so a promoted id bump is not reported as a difference
while a current route that still seats a superseded id is.

Tool version 2 added the two history-free DEBUGGING routes. A version-1
baseline does not hold them; they are then listed as not compared rather than
as differences — their outcome still reaches the comparison through the
prior-failure requests that baseline recorded.

Model ids never appear in this file: the one id a request needs is read off the
registry by key.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Hermetic before the router is imported: the router may read local model
# state at import or route time, and a developer's shell must not leak in.
_STATE_DIR = tempfile.mkdtemp(prefix="dmr-stateless-")
atexit.register(shutil.rmtree, _STATE_DIR, True)
os.environ["DEEP_MODEL_ROUTER_OVERLAY"] = "off"
os.environ["DEEP_MODEL_ROUTER_AUTOUPGRADE"] = "0"
os.environ["DEEP_MODEL_ROUTER_STATE_DIR"] = _STATE_DIR

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import route_task  # noqa: E402

TOOL_VERSION = 2
DECISION_FIELDS = (
    "selected_role", "selected_model",
    "selected_effort", "selected_effort_effective", "selected_effort_native",
    "review", "dispatch_seats",
    "fallbacks_applied", "fallback_compensations_applied",
    "requires_human_confirmation", "human_confirmation_deferred",
    "human_control_causes", "terminal",
)
BANDS = (("LOW", (0, 0, 0, 0)), ("MEDIUM", (2, 1, 1, 1)),
         ("HIGH", (2, 2, 1, 1)), ("CRITICAL", (2, 2, 2, 2)))
RUNTIMES = ("claude_code", "codex", "grok")
FLAG_SETS = ((), ("security_sensitive",), ("production_hotfix",), ("bridge_down",))


def _request(task_class, dims, runtime, flags=(), **extra):
    c, u, b, r = dims
    req = {"route_schema_version": route_task.ROUTE_SCHEMA_VERSION,
           "task_class": task_class, "complexity": c, "uncertainty": u,
           "blast_radius": b, "reversibility": r, "runtime": runtime,
           "flags": list(flags)}
    req.update(extra)
    return req


def _run(request: dict) -> tuple[int, dict | None, str]:
    """One route through the CLI entry point: (exit, parsed stdout, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(request, fh)
        path = fh.name
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = route_task.main(["--request-json", path, "--format", "json"])
    finally:
        os.unlink(path)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def _decision(code: int, result: dict | None, stderr: str) -> dict:
    record: dict = {"exit": code}
    if result is None:
        record["stderr"] = stderr.strip().splitlines()[-1:] if stderr else []
        return record
    for key in DECISION_FIELDS:
        if key in result:
            record[key] = result[key]
    if result.get("model_overlay") is not None:
        record["model_overlay"] = result["model_overlay"]
    return record


HISTORY_FREE_SINCE = {"DEBUGGING/claude_code/history_free": 2,
                      "DEBUGGING/codex/history_free": 2}


def requests() -> list[tuple[str, dict]]:
    """The 54 named requests, in a fixed order. DEBUGGING's prior model is
    the selected model of the same request without history, so that
    history-free route is routed first and recorded too."""
    out: list[tuple[str, dict]] = []
    for band, dims in BANDS:
        for runtime in RUNTIMES:
            for flags in FLAG_SETS:
                name = f"IMPLEMENTATION/{band}/{runtime}/{'+'.join(flags) or 'none'}"
                out.append((name, _request("IMPLEMENTATION", dims, runtime, flags)))
    review_dims = (2, 2, 2, 1)
    out.append(("REVIEW/claude_code/no_context",
                _request("REVIEW", review_dims, "claude_code")))
    author = route_task.load_config()["models"]["claude_senior"]["id"]
    out.append(("REVIEW/claude_code/context_claude_senior",
                _request("REVIEW", review_dims, "claude_code", review_context={
                    "target_sha256": "0" * 64,
                    "author_model_ids": [author],
                    "author_families": []})))
    for runtime in ("claude_code", "codex"):
        base = _request("DEBUGGING", (1, 2, 1, 1), runtime)
        code, result, err = _run(base)
        if result is None or not result.get("selected_model"):
            raise SystemExit(f"DEBUGGING/{runtime}: the history-free route selected "
                             f"no model (exit {code}): {err.strip()}")
        out.append((f"DEBUGGING/{runtime}/history_free", base))
        out.append((f"DEBUGGING/{runtime}/prior_failure_on_selected",
                    {**base, "prior_failures": [result["selected_model"]]}))
    assert len(out) == 54, len(out)
    return out


def collect() -> dict:
    routes, digests, versions = [], set(), set()
    for name, req in requests():
        code, result, err = _run(req)
        if result is not None:
            digests.add(result.get("policy_sha256"))
            versions.add(result.get("router_plugin_version"))
        routes.append({"name": name, "request": req, "decision": _decision(code, result, err)})
    if len(digests) != 1:
        raise SystemExit(f"routes disagree on policy_sha256: {sorted(map(str, digests))}")
    return {"tool": "check_stateless_routes", "tool_version": TOOL_VERSION,
            "router_plugin_version": versions.pop() if len(versions) == 1 else sorted(versions),
            "policy_sha256": digests.pop(), "routes": routes}


def _substitution(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    table: dict[str, str] = {}
    for key, chain in data["chains"].items():
        for old in chain[:-1]:
            table[old] = chain[-1]
    return table


def _rewrite(node, table: dict[str, str]):
    if isinstance(node, str):
        return table.get(node, node)
    if isinstance(node, list):
        return [_rewrite(v, table) for v in node]
    if isinstance(node, dict):
        return {k: _rewrite(v, table) for k, v in node.items()}
    return node


def _diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            sub = f"{path}.{key}" if path else key
            if key not in a:
                out.append(f"{sub}: added {json.dumps(b[key])}")
            elif key not in b:
                out.append(f"{sub}: removed (was {json.dumps(a[key])})")
            else:
                out.extend(_diff(a[key], b[key], sub))
        return out
    if a != b:
        return [f"{path}: {json.dumps(a)} -> {json.dumps(b)}"]
    return []


def compare(baseline: dict, current: dict, table: dict[str, str]) -> tuple[list[str], list[str]]:
    """(problems, not_compared). Only the baseline is rewritten: a current
    route seating a superseded id must differ from the rewritten baseline."""
    def view(r, rewrite):
        rec = {"request": r["request"], "decision": r["decision"]}
        return _rewrite(rec, table) if rewrite else rec
    old = {r["name"]: view(r, True) for r in baseline["routes"]}
    new = {r["name"]: view(r, False) for r in current["routes"]}
    version = baseline.get("tool_version", 1)
    problems = [f"{n}: missing from current run" for n in old if n not in new]
    skipped = [n for n in new if n not in old and HISTORY_FREE_SINCE.get(n, 0) > version]
    problems += [f"{n}: not in baseline" for n in new if n not in old and n not in skipped]
    for name in (n for n in old if n in new):
        problems.extend(f"{name}: {d}" for d in _diff(old[name], new[name]))
    return problems, skipped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, help="write the decision baseline JSON here")
    p.add_argument("--compare", type=Path, help="baseline JSON to compare the current run with")
    p.add_argument("--id-succession", type=Path,
                   help="id-succession.json whose chains map superseded ids to current ones")
    args = p.parse_args(argv)
    current = collect()
    if args.out:
        args.out.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.compare is None:
        if not args.out:
            print(json.dumps(current, indent=2, sort_keys=True))
        else:
            print(f"wrote {len(current['routes'])} routes, policy_sha256 {current['policy_sha256']}")
        return 0
    baseline = json.loads(args.compare.read_text(encoding="utf-8"))
    problems, skipped = compare(baseline, current, _substitution(args.id_succession))
    print(f"routes: {len(current['routes'])}; policy_sha256 {baseline['policy_sha256']} -> "
          f"{current['policy_sha256']}")
    for name in skipped:
        print(f"  not compared (baseline tool_version {baseline.get('tool_version', 1)} "
              f"predates it): {name}")
    if problems:
        print(f"{len(problems)} decision-field difference(s):")
        for line in problems:
            print(f"  {line}")
        return 1
    print("no decision-field differences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
