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
    before = len(Policy._cache)
    for i in range(1, 26):
        cfg["router"]["confidence"]["escalate_below"] = 0.5 + i * 0.001
        route(task, cfg)
    assert len(Policy._cache) - before == 1, (
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
