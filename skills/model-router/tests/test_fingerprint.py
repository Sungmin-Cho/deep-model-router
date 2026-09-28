"""design §4 B1 + §5 (a)~(i): fingerprint 정규형의 계약 테스트.

정규형은 route가 각 입력을 소비하는 의미론을 그대로 따른다: set-의미
리스트는 dedup+sort, isolation_evidence는 strip 포함, prior_models는
multiplicity 보존, local_policy는 bool(lp) 의미론(falsy -> null).
"""
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import (  # noqa: E402
    Task, default_config, load_config, route, request_sha256_of,
)
from policy_digest import canonical_policy_sha256  # noqa: E402
import copy  # noqa: E402

_CFG = default_config()
ID = lambda key: _CFG["models"][key]["id"]                       # noqa: E731

BASE = dict(task_class="IMPLEMENTATION", complexity=1, uncertainty=1,
            blast_radius=1, reversibility=0)


def _fp(**overrides):
    return request_sha256_of(Task(**{**BASE, **overrides}))


def test_same_request_twice_is_identical():                       # (a)
    assert _fp() == _fp()


def test_flag_order_and_duplicates_converge():                    # (b)(c)
    assert _fp(flags=["large_context", "tool_heavy"]) \
        == _fp(flags=["tool_heavy", "large_context", "tool_heavy"])


def test_isolation_evidence_whitespace_and_dups_converge():       # (d)
    assert _fp(isolation_evidence=["s1 ", " s2"]) \
        == _fp(isolation_evidence=["s2", "s1", "s1"])


def test_local_policy_absent_empty_and_all_null():                # (e)
    absent = _fp()
    empty = _fp(_local_policy={})
    nulls = _fp(_local_policy={
        "minimum_capability_tier": None, "minimum_effort": None,
        "minimum_reviewers": None, "minimum_provider_families": None,
        "allowed_families": None})
    assert absent == empty            # bool(lp) 의미론: 둘 다 미적용
    assert absent != nulls            # local_policy_applied False vs True


def test_unavailable_lists_and_allowed_families_converge():
    a = _fp(unavailable_models=[ID("xai_frontier"), ID("openai_worker_fast")],
            _local_policy={"allowed_families": ["openai", "claude", "openai"]})
    b = _fp(unavailable_models=[ID("openai_worker_fast"), ID("xai_frontier"), ID("xai_frontier")],
            _local_policy={"allowed_families": ["claude", "openai"]})
    assert a == b


def test_prior_models_multiplicity_is_preserved():
    once = _fp(prior_failures=1, prior_models=[ID("openai_worker_fast")])
    twice = _fp(prior_failures=2, prior_models=[ID("openai_worker_fast"), ID("openai_worker_fast")])
    assert once != twice


def test_route_emits_both_fields_and_terminal_keeps_them():       # (h)
    ok = route(Task(**BASE))
    assert len(ok["request_sha256"]) == 64
    assert len(ok["decision_fingerprint"]) == 64
    terminal = route(Task(**BASE, prior_failures=4,
                          prior_models=[ID("openai_worker_fast")] * 4))
    assert terminal["terminal"] is not None
    assert len(terminal["request_sha256"]) == 64
    assert len(terminal["decision_fingerprint"]) == 64


def test_input_change_changes_request_sha():                      # (i)
    assert _fp(complexity=1) != _fp(complexity=2)


def test_cfg_change_moves_policy_and_fingerprint_not_request():   # (g)
    cfg = default_config()
    injected = copy.deepcopy(cfg)
    injected["role_bindings"]["default"]["worker_fast"] = "claude_worker_fast"
    a, b = route(Task(**BASE), cfg), route(Task(**BASE), injected)
    assert a["request_sha256"] == b["request_sha256"]
    assert a["policy_sha256"] != b["policy_sha256"]
    assert a["decision_fingerprint"] != b["decision_fingerprint"]
    assert b["policy_sha256"] == canonical_policy_sha256(injected)


def test_a_mutated_cfg_cannot_share_a_fingerprint_with_a_fresh_one():
    """round-1 review, 3/3 agreement: `policy_sha256` digests the cfg's CURRENT
    content while `Policy.of` cached derived semantics on `id(cfg)`, so routing
    the same object twice across an in-place mutation produced a decision built
    from stale policy — and the new fingerprint attested it to the post-mutation
    content. Same fingerprint, different decision breaks exactly what B1 claims.
    """
    # A task that actually seats claude_architect — the mutated row must be
    # one the decision reads, or the test proves nothing.
    task = Task(task_class="ARCHITECTURE", complexity=3, uncertainty=3,
                blast_radius=3, reversibility=2, flags=["review_disagreement"])
    cfg = load_config()
    baseline = route(task, cfg)                        # seats the cache
    cfg["models"]["claude_architect"]["capability_tier"] = 0
    stale = route(task, cfg)

    fresh_cfg = load_config()
    fresh_cfg["models"]["claude_architect"]["capability_tier"] = 0
    fresh = route(task, fresh_cfg)

    # The mutated row must reach the decision, asserted rather than assumed:
    # the day this fixture stops seating claude_architect, `stale == fresh`
    # starts passing vacuously and this guard disappears in silence — the
    # same failure shape the B5 positive test was fixed for.
    assert fresh["selected_model"] == baseline["selected_model"]
    assert baseline["selected_capability_tier"] == 3
    assert fresh["selected_capability_tier"] == 0

    assert stale["policy_sha256"] == fresh["policy_sha256"]
    assert stale["decision_fingerprint"] == fresh["decision_fingerprint"]
    assert stale == fresh, (
        "one fingerprint must identify one decision; the cfg content is "
        "identical, so every emitted field must be too")


def test_repeated_mutation_of_one_cfg_keeps_the_policy_cache_bounded():
    """round-2 review, 3/3 agreement: keying the Policy cache on
    (identity, digest) fixed the staleness but made a long-lived library
    process retain one Policy per historical revision of the same config
    object. One identity keeps ONE entry — the current revision — which also
    keeps the strong `self.cfg` reference that stops the id being recycled.
    """
    from route_task import Policy
    task = Task(**BASE)
    cfg = load_config()
    for i in range(1, 26):
        cfg["router"]["confidence"]["escalate_below"] = 0.5 + i * 0.001
        route(task, cfg)
    # Counted per config object, not as a length delta: the cache is also
    # bounded overall (LRU, design 2026-09-25 DD-A2), so a full cache stays
    # the same length while still admitting this object's one entry.
    held = [p for _, p in Policy._cache.values() if p.cfg is cfg]
    assert len(held) == 1, (
        "25 revisions of one config object must not leave 25 cached policies")
    # Bounded, and still correct: the last revision routes like a fresh load.
    fresh = load_config()
    fresh["router"]["confidence"]["escalate_below"] = 0.5 + 25 * 0.001
    assert route(task, cfg) == route(task, fresh)


def test_the_cache_key_digest_is_the_digest_the_route_emits():
    """round-2 review A4/I2: the cache key and the emitted `policy_sha256`
    used to be two independent canonical dumps of the same object, agreeing
    only because nothing happened between them. One computation now feeds
    both, so they cannot diverge."""
    from route_task import Policy
    cfg = load_config()
    out = route(Task(**BASE), cfg)
    assert Policy.of(cfg).content_sha == out["policy_sha256"]


def test_policy_pin_is_omitted_from_the_request_identity():
    """Design 2026-09-25 DD-A9: a pin selects the policy — which the digest
    already records — so it never enters request_sha256. Pinned from the
    pre-pin canonical payload: absent and present both hash to it."""
    from route_task import task_from_request_v1
    request = {"route_schema_version": 1, "task_class": "IMPLEMENTATION",
               "complexity": 2, "uncertainty": 1, "blast_radius": 1, "reversibility": 1}
    golden = "7b50c0cd1c25f55c41c79cb9f14a1c24cfe6dd52bea9d155775367ae6aa16c8a"
    assert request_sha256_of(task_from_request_v1(request)) == golden
    pinned = task_from_request_v1({**request, "policy_pin": "d" * 64})
    assert pinned._policy_pin == "d" * 64
    assert request_sha256_of(pinned) == golden


# Part B (1.17.0) request fields — design 2026-09-25 "요청 해시 규칙(Part B 공통)".
# Pinned from the canonical payload 1.16.1 computed for this request, before any
# Part B field existed. Every Part B field is omitted from the canonical dict
# when absent, so a stored fingerprint from 1.16.x still matches the same
# request; each rule task adds its field to the "absent" half of this test.
PART_B_GOLDEN = "4cbb7137c9b57f568533b3ac1830050c85cb7a33f20da3c25b4b1f78d3aa72e5"


def _part_b_request():
    return {"route_schema_version": 1, "task_class": "IMPLEMENTATION", "complexity": 2,
            "uncertainty": 1, "blast_radius": 1, "reversibility": 1, "runtime": "codex",
            "flags": ["security_sensitive"],
            "availability_snapshot": {"unavailable_models": [ID("xai_frontier")],
                                      "isolation": "available"},
            "local_policy": {"allowed_families": ["openai", "claude"], "minimum_reviewers": 1},
            "attempt_outcomes": [{"attempt_id": "a1", "model_id": ID("openai_worker_fast"),
                                  "kind": "capability_failure", "evidence_sha256": "1" * 64}]}


def _request_sha(request):
    # Through `route`: the digest is taken after validation normalises the
    # request (attempt records gain their explicit null recovery field).
    from route_task import task_from_request_v1
    return route(task_from_request_v1(request), _CFG)["request_sha256"]


def test_part_b_fields_absent_preserve_the_1161_request_hash():
    assert _request_sha(_part_b_request()) == PART_B_GOLDEN


def test_part_b_fields_present_move_the_request_hash():
    """The other half: a declared field is request content, so it must move
    the identity — a hash that ignores a field that changes the route lets two
    decisions share a fingerprint."""
    def with_attempt(**fields):
        return lambda r: {**r, "attempt_outcomes": [{**r["attempt_outcomes"][0], **fields}]}

    variants = {
        "implementer": lambda r: {**r, "implementer": {"model_id": ID("claude_senior")}},
        "checks_available": lambda r: {**r, "availability_snapshot": {
            **r["availability_snapshot"], "checks_available": False}},
        "attempt effort": with_attempt(effort="HIGH"),
        "attempt retry evidence": with_attempt(effort="HIGH", retry_evidence_sha256="2" * 64),
        "family_quota": lambda r: {**r, "availability_snapshot": {
            **r["availability_snapshot"], "family_quota": {"openai": "low"}}},
    }
    seen = {PART_B_GOLDEN}
    for name, build in variants.items():
        digest = _request_sha(build(_part_b_request()))
        assert digest not in seen, name
        seen.add(digest)
