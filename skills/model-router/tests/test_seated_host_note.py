"""The seated-host note (issue #53): a declared host model that holds a review
seat while no author is declared gets one advisory note. The seats themselves
never move — the router does not infer authorship from the host.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""
import copy
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

import route_task as rt  # noqa: E402

CFG = rt.load_config()
ID = lambda key: CFG["models"][key]["id"]  # noqa: E731
DIGEST = "a" * 64


def _route(**extra):
    req = {"route_schema_version": 1, "task_class": "IMPLEMENTATION", "complexity": 2,
           "uncertainty": 2, "blast_radius": 1, "reversibility": 1, "runtime": "claude_code",
           "flags": []}
    req.update(copy.deepcopy(extra))
    return rt.route(rt.task_from_request_v1(req), CFG)


def _notes(out):
    return [n for n in out["notes"] if n.startswith("host model ")]


def _seat_models(out):
    if "dispatch_seats" in out:
        return [s["model_id"] for s in out["dispatch_seats"]]
    return list(out["review"]["reviewer_models"])


def test_a_seated_host_without_a_declaration_gets_one_note_and_keeps_its_seat():
    host = ID("claude_senior")
    bare = _route()
    out = _route(host_seat={"model": host})
    assert host in out["review"]["reviewer_models"]
    seat = f"reviewer-{out['review']['reviewer_models'].index(host) + 1}"
    assert _notes(out) == [
        f"host model {host} is seated as {seat} and no author is declared: if this session "
        "wrote the work under review, declare it (implementer.model_id, or "
        "review_context.author_model_ids for a REVIEW task) and route again; the router "
        "never infers authorship from the host"]
    assert _seat_models(out) == _seat_models(bare)
    assert out["selected_model"] == bare["selected_model"]


def test_declaring_the_implementer_reseats_and_drops_the_note():
    host = ID("claude_senior")
    out = _route(host_seat={"model": host}, implementer={"model_id": host})
    assert host not in _seat_models(out)
    assert _notes(out) == []


def test_no_note_without_a_host_or_when_the_host_is_not_seated():
    assert _notes(_route()) == []
    out = _route(host_seat={"model": ID("claude_worker_fast")})
    assert ID("claude_worker_fast") not in _seat_models(out)
    assert _notes(out) == []


RUNTIME_OF = {"claude": "claude_code", "openai": "codex", "xai": "grok"}
REVIEW = dict(task_class="REVIEW", complexity=2, uncertainty=2, blast_radius=1, reversibility=1)


def _family(model_id):
    return next(m["family"] for m in CFG["models"].values() if m["id"] == model_id)


def test_a_review_task_whose_lead_is_the_host_is_noted_until_context_is_declared():
    """A REVIEW task's lead is its executor (1.17.0): a host that wrote the
    target and leads its review reviews itself unless review_context says so."""
    lead = _route(**REVIEW)["dispatch_seats"][0]["model_id"]
    runtime = RUNTIME_OF[_family(lead)]
    out = _route(host_seat={"model": lead}, **{**REVIEW, "runtime": runtime})
    assert out["dispatch_seats"][0]["model_id"] == lead
    assert len(_notes(out)) == 1
    assert _notes(out)[0].startswith(f"host model {lead} is seated as reviewer-1 ")
    declared = _route(host_seat={"model": lead}, **{**REVIEW, "runtime": runtime},
                      review_context={"target_sha256": DIGEST, "author_model_ids": [lead],
                                      "author_families": []})
    assert lead not in _seat_models(declared)
    assert _notes(declared) == []


def test_a_terminal_route_carries_no_note():
    host = ID("claude_senior")
    worker = _route()["selected_model"]
    out = _route(host_seat={"model": host}, prior_failures=[worker] * 5)
    assert out["terminal"] is not None
    assert _notes(out) == []


def test_a_context_variant_of_the_host_id_is_the_same_model():
    """A session reports `<id>[1m]` for the 1M-context variant (review i1)."""
    host = ID("claude_senior")
    out = _route(host_seat={"model": f"{host}[1m]"})
    assert len(_notes(out)) == 1 and f"host model {host}[1m] is seated as " in _notes(out)[0]


def test_the_cli_host_model_flag_reaches_the_note():
    import json
    import subprocess
    host = ID("claude_senior")
    proc = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "route_task.py"), "--class", "IMPLEMENTATION",
         "--complexity", "2", "--uncertainty", "2", "--blast-radius", "1", "--reversibility", "1",
         "--runtime", "claude_code", "--host-model", host, "--format", "json"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    notes = [n for n in json.loads(proc.stdout)["notes"] if n.startswith("host model ")]
    assert len(notes) == 1 and notes[0].startswith(f"host model {host} is seated as ")
