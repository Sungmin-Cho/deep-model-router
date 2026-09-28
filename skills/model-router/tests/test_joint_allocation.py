"""Joint assignment uses real candidate supply without inventing source authors."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import route_task as rt
CFG = rt.default_config()


def run(**over):
    payload = dict(route_schema_version=1, task_class='REVIEW', complexity=2,
                   uncertainty=2, blast_radius=1, reversibility=0, runtime='codex',
                   review_context=dict(target_sha256='a'*64,
                       author_model_ids=[CFG['models']['claude_architect']['id']]))
    payload.update(over)
    return rt.route(rt.task_from_request_v1(payload), CFG)


@pytest.mark.parametrize('runtime', list(CFG['runtimes']))
def test_existing_artifact_executor_is_one_of_its_reviewers(runtime):
    out = run(runtime=runtime)
    assert out['terminal'] is None
    rv = out['review']
    assert out['selected_role'] in rv['reviewers']
    assert rv['reviewer_models'].count(out['selected_model']) == 1
    assert len(rv['reviewer_models']) == len(set(rv['reviewer_models'])) == 2
    assert not rv['independence_compromised']
    assert not out['fallbacks_applied']


def test_source_review_can_reserve_frontier_for_judge():
    out = run(flags=['review_disagreement'])
    assert out['terminal'] is None
    rv = out['review']
    assert rv['judge_model'] == CFG['models']['openai_frontier']['id']
    assert rv['judge_model'] not in rv['reviewer_models']
    assert not rv['judge_unavailable']
    assert not rv['review_depth_reduced']


def test_insufficient_author_eligible_supply_keeps_a_gate():
    out = run(review_context=dict(target_sha256='a'*64, author_families=['openai'], author_model_ids=[CFG['models']['claude_architect']['id']]))
    assert out['terminal'] or out['requires_human_confirmation']


def test_ordinary_disagreement_uses_hidden_frontier_without_losing_diversity():
    out = run(review_context=None, flags=['review_disagreement'])
    rv = out['review']
    assert out['terminal'] is None and not rv['judge_unavailable']
    assert len({out['selected_model'], *rv['reviewer_models'], rv['judge_model']}) == 4
    assert out['cross_family_review']
    assert not out['fallbacks_applied']


def test_source_dispatch_list_executes_lead_once_and_binds_effort():
    out = run(flags=['review_disagreement'])
    seats = out['dispatch_seats']
    assert [s['seat'] for s in seats] == ['reviewer-1', 'reviewer-2', 'judge']
    assert len({s['model_id'] for s in seats}) == 3
    assert seats[0]['model_id'] == out['selected_model']
    policy = rt.Policy(CFG)
    for seat in seats:
        assert seat['effort_native'] == policy.native_effort(seat['model_id'], seat['effort'])


def test_promoted_review_band_can_promote_the_source_lead():
    # MEDIUM (6), promoted by an unknown root cause plus a real outage (0.79).
    # Uncertainty cannot be the signal any more: since DD-B2 an uncertainty
    # that lifted the band does not also promote it.
    out = run(uncertainty=1, flags=['unknown_root_cause'],
              availability_snapshot=dict(unavailable_models=[CFG['models']['xai_frontier']['id']]))
    assert out['risk_band'] == 'MEDIUM'

    assert out['review']['band'] == 'HIGH'
    assert out['terminal'] is None
    assert not out['review']['review_depth_reduced']
    assert out['selected_model'] in out['review']['reviewer_models']


def test_missing_judge_does_not_misreport_missing_source_reviewers():
    out = run(flags=['review_disagreement'], review_context=dict(target_sha256='a'*64,
        author_model_ids=[CFG['models'][k]['id'] for k in ['claude_architect','openai_frontier']]))
    assert out['terminal'] is None
    assert out['review']['judge_unavailable']
    assert not out['review']['independence_compromised']
    assert len(set(out['review']['reviewer_models'])) == 2
    assert out['requires_human_confirmation']


def test_equal_tier_frontier_replacement_does_not_invent_downgrade_compensation():
    out = run(flags=['review_disagreement'], review_context=dict(target_sha256='a'*64,
        author_model_ids=[CFG['models']['openai_worker_fast']['id']]),
        availability_snapshot=dict(unavailable_models=[CFG['models']['claude_architect']['id']]))
    assert out['terminal'] is None
    assert len(out['review']['reviewers']) == 2
    assert out['review']['judge_model'] == CFG['models']['openai_frontier']['id']
    assert 'raise_effort_to_MAX_and_add_second_review' not in out['fallback_compensations_applied']
    assert out['fallbacks_applied']


def test_source_lead_can_use_supply_hidden_from_its_preliminary_role():
    astra = CFG['models']['openai_frontier']['id']
    out = run(complexity=0, uncertainty=0, blast_radius=0,
              availability_snapshot=dict(unavailable_models=[m['id'] for m in CFG['models'].values() if m['id'] != astra]))
    assert out['terminal'] is None
    assert out['selected_model'] == astra
    assert len(out['dispatch_seats']) == 1


def test_single_source_reviewer_prefers_a_family_distinct_from_authors():
    out = run(complexity=1, uncertainty=1, blast_radius=1, flags=['latency_sensitive'],
        review_context=dict(target_sha256='a'*64, author_model_ids=[CFG['models']['claude_senior']['id']]))
    assert out['terminal'] is None
    assert out['cross_family_review']
    families = {m['id']:m['family'] for m in CFG['models'].values()}
    assert families[out['selected_model']] != 'claude'


@pytest.mark.parametrize('local', [dict(minimum_capability_tier=3), dict(minimum_effort='MAX')])
def test_source_lead_search_respects_local_requirements(local):
    out = run(complexity=1, uncertainty=1, blast_radius=1, local_policy=local)
    assert out['terminal'] is None
    policy = rt.Policy(CFG)
    if 'minimum_capability_tier' in local:
        assert policy.tier_of[out['selected_model']] >= local['minimum_capability_tier']
    if 'minimum_effort' in local:
        assert out['selected_effort_effective'] == local['minimum_effort']


def test_source_compensation_does_not_describe_a_discarded_secondary_seat():
    out = run(complexity=1, uncertainty=1, blast_radius=1,
        availability_snapshot=dict(unavailable_models=[m['id'] for m in CFG['models'].values() if m['family']=='openai']))
    assert out['terminal'] is None
    assert not out['fallbacks_applied']
    assert not out['fallback_compensations_applied']


def test_joint_search_never_drops_a_requested_review_slot():
    import copy
    cfg = copy.deepcopy(CFG)
    cfg['review']['HIGH']['reviewers'] = list(cfg['role_tiers'])
    task = rt.Task(task_class='IMPLEMENTATION', complexity=2, uncertainty=2,
                   blast_radius=1, reversibility=0, runtime='claude_code')
    out = rt.route(task, cfg)
    assert out['terminal'] or len(out['review']['reviewers']) == len(cfg['role_tiers'])


def test_nullable_local_floors_remain_absent_for_joint_search():
    out = run(local_policy=dict(minimum_capability_tier=None, minimum_effort=None,
                                minimum_provider_families=None))
    assert out['terminal'] is None
