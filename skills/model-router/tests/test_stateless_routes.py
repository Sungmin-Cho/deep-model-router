"""scripts/tools/check_stateless_routes.py (plan §0.2): the decision-field
oracle a release is compared with. Always run as a subprocess — the tool pins
its own hermetic environment at import. i1r1 opus F9: `--id-succession`
rewrites the BASELINE side only, and the history-free DEBUGGING routes the
prior-failure requests are built from are recorded and compared."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "scripts" / "tools" / "check_stateless_routes.py"


def _tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)],
                          capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def current(tmp_path_factory):
    out = tmp_path_factory.mktemp("stateless") / "current.json"
    proc = _tool("--out", out)
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def test_the_history_free_debugging_routes_are_recorded(current):
    by_name = {r["name"]: r for r in current["routes"]}
    for runtime in ("claude_code", "codex"):
        free = by_name[f"DEBUGGING/{runtime}/history_free"]
        prior = by_name[f"DEBUGGING/{runtime}/prior_failure_on_selected"]
        assert "prior_failures" not in free["request"]
        assert prior["request"]["prior_failures"] == [free["decision"]["selected_model"]]
    assert current["tool_version"] == 2


def test_a_run_compares_equal_to_itself(current, tmp_path):
    proc = _tool("--compare", _write(tmp_path, "b.json", current))
    assert proc.returncode == 0 and "no decision-field differences" in proc.stdout, proc.stdout


def test_a_current_route_seating_a_superseded_id_is_a_difference(current, tmp_path):
    """Rewriting both sides let a current route that still seats a
    superseded id compare equal. The chain here marks the id the current run
    seats as superseded; only the baseline side moves, so it must show."""
    seated = next(r["decision"]["selected_model"] for r in current["routes"]
                  if r["decision"].get("selected_model"))
    chains = {"chains": {"demo_key": [seated, "demo-successor-id"]}}
    proc = _tool("--compare", _write(tmp_path, "b.json", current),
                 "--id-succession", _write(tmp_path, "s.json", chains))
    assert proc.returncode == 1, proc.stdout
    assert f'"demo-successor-id" -> "{seated}"' in proc.stdout


def test_a_v1_baseline_is_compared_on_the_requests_it_recorded(current, tmp_path):
    """A tool_version 1 baseline has no history-free routes; their outcome is
    still compared through the prior-failure REQUESTS the baseline recorded."""
    old = copy.deepcopy(current)
    old["tool_version"] = 1
    old["routes"] = [r for r in old["routes"] if not r["name"].endswith("/history_free")]
    proc = _tool("--compare", _write(tmp_path, "v1.json", old))
    assert proc.returncode == 0, proc.stdout
    assert "not in baseline" not in proc.stdout
    moved = copy.deepcopy(old)
    dbg = next(r for r in moved["routes"] if r["name"].startswith("DEBUGGING/"))
    dbg["request"]["prior_failures"] = ["demo-other-id"]
    proc = _tool("--compare", _write(tmp_path, "v1m.json", moved))
    assert proc.returncode == 1 and "request.prior_failures" in proc.stdout, proc.stdout
