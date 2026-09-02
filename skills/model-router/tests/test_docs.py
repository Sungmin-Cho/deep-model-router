"""Static contract tests over the skill's documents and config.

These exist because three research documents (docs/research/, 2026-08-15)
found the shipped documents making claims the artifact does not keep: every
documented invocation was cwd-dependent, templates omitted prompts, and the
observability contract listed fields nothing produces. Each test here pins
one of those contracts.

Run:  python3 -m pytest skills/model-router/tests/ -q
"""

import re
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import load_config  # noqa: E402

CFG = load_config()


# ---------------------------------------------------------------------------
# D1 — documented invocations must not depend on the caller's cwd
# (docs/design/2026-08-15-dispatch-layer-design.md, DD-1)
# ---------------------------------------------------------------------------

def test_docs_never_invoke_the_router_cwd_relative():
    """`python3 scripts/route_task.py` only works with cwd = skill root, which
    no caller is guaranteed — a background subagent inherits the project
    root. Worse, the failure exits 2, which the contract reserves for
    "invalid input". Every documented invocation carries the skill-root
    prefix instead."""
    for doc in (SKILL / "SKILL.md", SKILL / "references" / "examples.md"):
        text = doc.read_text()
        assert not re.search(r"python3 scripts/route_task\.py", text), doc.name
        assert '"$SKILL_DIR"/scripts/route_task.py' in text, doc.name
        assert re.search(r"^SKILL_DIR=", text, re.MULTILINE), doc.name


# ---------------------------------------------------------------------------
# F-06 / T2 — every bridge template must carry a quoted prompt, no shell
# hazards (DD-4)
# ---------------------------------------------------------------------------

def _mechanisms(spec):
    """Every machine string in one transport entry, seat-aware.

    A transport carries either a single `mechanism` or one `mechanism_<seat>`
    per seat profile (2026-08-25 design DD-4: a read-only reviewer and a
    write-capable maker need opposite tool surfaces, so one string cannot
    serve both). Collecting whatever keys are PRESENT is deliberate: a seat
    that did not pass its shipping gate is expressed by omitting its key, and
    the invariants below must hold conditionally over what actually ships
    rather than demanding a string that intentionally does not exist.
    """
    return {k: v for k, v in spec.items() if k == "mechanism"
            or k.startswith("mechanism_")}


def test_every_bridge_mechanism_delivers_a_prompt_safely_and_has_no_pipe():
    """A mechanism with no prompt argument at all waits on stdin — a
    background shell with stdin open never reaches the model, and to the
    caller that is indistinguishable from an unresponsive model.

    There are now two safe shapes, not one. A quoted `"<prompt>"` keeps the
    prompt in argv without word-splitting (claude/codex). `--prompt-file
    /dev/stdin` takes it out of argv entirely (the grok seats): safety there
    comes not from the string but from the supervisor, which opens the
    `--prompt-file` and wires it to the child's stdin — and passes
    /dev/null when no prompt file is declared, so waiting on stdin is
    structurally impossible either way. That is why the original rationale
    ("a string with no prompt argument waits on stdin") no longer decides
    this on its own: the grok strings wait on stdin ON PURPOSE.

    A literal alternation inside a template is a shell pipe when pasted, in
    any shape. And every `codex exec` mechanism must still carry a sandbox
    slot — DD-4's permission-pinning rule applies to all three hosts that
    bridge into openai, not just the ones written first.
    """
    for host, entries in CFG["transports"].items():
        for name, spec in entries.items():
            if name == "native":
                continue
            mechanisms = _mechanisms(spec)
            assert mechanisms, (host, name, "no mechanism string at all")
            for key, mech in mechanisms.items():
                where = (host, name, key, mech)
                assert '"<prompt>"' in mech \
                    or "--prompt-file /dev/stdin" in mech, where
                assert "|" not in mech, where
                if "codex exec" in mech:
                    assert "-s <sandbox>" in mech, where


def test_grok_seat_profiles_pin_the_probed_tokens():
    """The recipes are what the probe ledger actually verified, so the tokens
    that carry the safety are pinned rather than left to drift.

    Reviewer: `--tools` removes the terminal tool at the source (a tool the
    model cannot see cannot cancel the turn), `--deny MCPTool` closes the
    meta-tool `--tools` leaves behind, and `--output-format json` is what
    makes the supervisor's envelope gate possible at all.

    Maker: required. Argv-level hard-link denial still fails; shipping
    depends on the supervisor's grok-maker-v1 contract (single-linked
    child cwd, per-attempt GROK_HOME, custom fail-closed profile,
    ProfileApplied.enforced). `mechanism_reviewer` remains required
    independently: issue #14's misjudgement is removed by the reviewer
    seat plus the envelope and artifact contracts, with or without a maker.
    """
    for host in ("claude_code", "codex"):
        spec = CFG["transports"][host]["to_xai"]
        reviewer = spec["mechanism_reviewer"]
        for token in ("--output-format json", "-s <fresh-uuid>",
                      "--tools read_file,list_dir,grep", "--deny MCPTool",
                      "--disable-web-search", "--prompt-file /dev/stdin"):
            assert token in reviewer, (host, token, reviewer)
        assert "-p " not in reviewer, host
        maker = spec["mechanism_maker"]
        for token in ("--agent", '--allow "Write(./**)"',
                      '--allow "Edit(./**)"', "--output-format json",
                      "--prompt-file /dev/stdin", "--deny MCPTool",
                      "--disallowed-tools Agent", "--no-subagents",
                      "--sandbox dmr-maker-v1",
                      "search_replace"):
            # Quoted: the parentheses in a rule argument are shell
            # metacharacters, so an unquoted form is a syntax error the
            # moment anyone pastes it.
            assert token in maker, (host, token, maker)


def test_adapters_fences_mirror_the_grok_seat_strings():
    """"Fences mirror the YAML" is an existing contract; this pins it for the
    two strings this tranche replaces. Token-wise, not byte-wise: the fence
    wraps for width, so a whitespace-insensitive comparison is the honest
    one. Scope is deliberately these two — generalizing to every fence in
    the file is a separate change."""
    text = (SKILL / "references" / "adapters.md").read_text()
    fence_tokens = set(text.split())
    for host in ("claude_code", "codex"):
        spec = CFG["transports"][host]["to_xai"]
        for key, mech in _mechanisms(spec).items():
            missing = [tok for tok in mech.split() if tok not in fence_tokens]
            assert not missing, (host, key, missing)


def _collapsed(text: str) -> str:
    """Whitespace-normalize so line-wrapped 'assumed' sentences cannot
    silently pass a negative pin."""
    return re.sub(r"\s+", " ", text)


def test_adapters_grok_section_wording():
    """Issue #16: grok-hosted bridges are verified from a darwin grok host;
    Codex → xai stays assumed; native grok isolation stays degraded."""
    adapters = _collapsed((SKILL / "references" / "adapters.md").read_text())
    profiles = _collapsed((SKILL / "references" / "model-profiles.md").read_text())
    yaml_text = _collapsed(re.sub(r"(?m)^# ?", "", (SKILL / "config" / "model-routing.yaml").read_text()))
    assert "grok → claude, grok → openai — assumed" not in adapters
    assert "verified (darwin grok host" in adapters
    assert "Codex → xai — assumed" in adapters or "Codex -> xai — assumed" in adapters
    assert "Assumed from this host" not in adapters
    assert "degraded unless both reviewers run as separate processes" in adapters
    assert "native `--agents` surface remains outside that claim" in adapters
    review_policy = _collapsed((SKILL / "references" / "review-policy.md").read_text())
    assert "Codex and grok native subagents carry an unverified assumption" in review_policy
    assert "grok → claude, and grok → openai use the same commands and are recorded as assumed" not in profiles
    assert "grok → claude, and grok → openai use the same commands and are recorded as assumed" not in yaml_text


# ---------------------------------------------------------------------------
# F-04 — the observability contract must not promise fields nothing produces
# (DD-3)
# ---------------------------------------------------------------------------

def test_routing_metrics_block_promises_only_what_the_router_emits():
    """`review_count` and `final_success` were listed under "Every route
    emits" with no producer anywhere in route_task.py. Pre-dispatch code
    cannot know either; they belong to the execution receipt."""
    text = (SKILL / "references" / "control-loop.md").read_text()
    block = re.search(r"routing_metrics:\n(.*?)```", text, re.S).group(1)
    for field in ("final_success", "review_count"):
        assert field not in block, field
        assert field in text, f"{field} must stay documented — as a receipt field"


# ---------------------------------------------------------------------------
# DD-5 / DD-8 — a seat that returns no verdict must be a defined outcome
# ---------------------------------------------------------------------------

def test_review_policy_defines_the_absent_reviewer():
    """The output contract admitted three verdicts and no absence, and the
    disagreement matrix was a 3x3 with no missing row — so the two natural
    moves (proceed on one verdict, or show it to a re-dispatched seat) were
    both the failure the independence rules exist to prevent."""
    text = (SKILL / "references" / "review-policy.md").read_text()
    assert "NO_RESPONSE" in text
    assert "### A seat that returns no verdict" in text
    # the matrix rows exist
    assert re.search(r"\|\s*any verdict\s*\|\s*`NO_RESPONSE`", text)
    assert re.search(r"\|\s*`NO_RESPONSE`\s*\|\s*`NO_RESPONSE`", text)
    # and the evidence-id source is stated
    assert "### Where the evidence id comes from" in text
    assert "attempt_id" in text


# ---------------------------------------------------------------------------
# DD-6 — dispatch failure must be an escalation trigger with decided
# accounting
# ---------------------------------------------------------------------------

def test_control_loop_escalates_on_silent_seats_and_decides_the_accounting():
    text = (SKILL / "references" / "control-loop.md").read_text()
    assert re.search(r"^13\.\s", text, re.M), "trigger 13 missing"
    assert "FAILED` with no parseable verdict block" in text, \
        "trigger 13 must name the same NO_RESPONSE members DD-5/Task 6 do, " \
        "except CANCELLED (deliberately excluded — see the CANCELLED " \
        "accounting clause below)"
    assert "### Accounting for silent seats" in text
    # the two decisions are stated, not left open
    assert "consumes one `max_review_rounds` round" in text
    assert "termination_confirmed: true" in text
    assert "--flags termination_unconfirmed" in text
    # CANCELLED joins the NO_RESPONSE/accounting vocabulary without becoming
    # an escalation trigger of its own
    assert "CANCELLED` never enters the ladder" in text


# ---------------------------------------------------------------------------
# DD-9 / DD-11 — the dispatch contract is documented where Layer B lives
# ---------------------------------------------------------------------------

def test_adapters_owns_a_dispatch_contract_section():
    text = (SKILL / "references" / "adapters.md").read_text()
    assert "## Dispatch contract" in text
    for required in ("dispatch_agent.py", "TERMINATION_UNCONFIRMED",
                     "Launch is not completion", "--prompt-file",
                     "deadline"):
        assert required in text, required


def test_skill_md_points_at_the_dispatch_layer():
    text = (SKILL / "SKILL.md").read_text()
    assert "## Dispatching the route" in text
    assert "verify-evidence" in text
    assert "termination_unconfirmed" in text
    # the frontmatter description gained the background triggers
    frontmatter = text.split("---")[1]
    assert "background" in frontmatter


def test_skill_md_points_at_observation_contract():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "references/observation.md" in text


def test_observation_md_invokes_validator_skill_dir_prefixed():
    path = SKILL / "references" / "observation.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"python3 scripts/validate_observation\.py", text)
    assert 'python3 "$SKILL_DIR/scripts/validate_observation.py"' in text
    assert "--root" in text
    assert "--check-refs" in text
    assert "--check-receipts" in text


# ---------------------------------------------------------------------------
# B6 / B7 / B8 — the documents' tables and lists are compared to the config
# cell by cell, not by substring
#
# The audit of 2026-08-18 found three drifts that every test in this file was
# structurally unable to see: SKILL.md gave REVIEW x CRITICAL two workers where
# the config gives one, both the SKILL.md and the routing-policy.md override
# lists claimed to be complete while omitting `concurrency_sensitive`, and the
# operational-flag list named one of the two flags the config declares. The
# checks above are substring checks, and a substring check cannot notice a
# missing row. These compare the parsed document against the parsed YAML, and
# they are the reason routing-policy.md and model-profiles.md are opened here
# at all — until now neither was.
# ---------------------------------------------------------------------------

SKILL_MD = (SKILL / "SKILL.md").read_text()
ROUTING_POLICY_MD = (SKILL / "references" / "routing-policy.md").read_text()
MODEL_PROFILES_MD = (SKILL / "references" / "model-profiles.md").read_text()


def _fenced_after(text: str, marker: str) -> str:
    """The first fenced block following `marker`."""
    assert marker in text, f"anchor missing: {marker!r}"
    tail = text[text.index(marker):]
    m = re.search(r"```\n(.*?)```", tail, re.S)
    assert m, f"no fenced block after {marker!r}"
    return m.group(1)


def _norm(s: str) -> str:
    """Documents hyphenate what the config spells with an underscore."""
    return s.replace("-", "_")


def _md_table(text: str, marker: str) -> tuple[list[str], list[list[str]]]:
    """(header cells, body rows) of the first pipe table after `marker`."""
    assert marker in text, f"anchor missing: {marker!r}"
    lines = text[text.index(marker):].splitlines()
    rows = []
    for line in lines:
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip("|").split("|")])
        elif rows:
            break
    assert len(rows) >= 3, f"no table after {marker!r}"
    return rows[0], rows[2:]


def test_skill_md_worker_table_matches_the_config_cell_by_cell():
    header, rows = _md_table(SKILL_MD, "### Worker by class and band")
    bands = [c.strip("`") for c in header[1:]]
    assert bands == sorted(CFG["router"]["bands"],
                           key=lambda b: CFG["router"]["bands"][b]["ordinal"])
    documented = {}
    for row in rows:
        task_class = row[0].strip("`")
        for band, cell in zip(bands, row[1:]):
            # Footnote markers carry prose, not policy: the cell is what routes.
            value = cell.replace("†", "").replace("‡", "by_reasoning_centric").strip()
            documented[(task_class, band)] = value
    actual = {(c, b): v for c, row in CFG["worker_selection"].items()
              for b, v in row.items()}
    assert documented == actual


def test_flag_groups_in_both_documents_match_the_config():
    """Every group, in both documents. `termination_unconfirmed` was declared,
    used, and explained in SKILL.md's own dispatch section while missing from
    its flag inventory — a document contradicting itself within one file."""
    block = _fenced_after(SKILL_MD, "**Flags** — detect all that apply:")
    skill_groups: dict[str, set[str]] = {}
    current = None
    for line in block.splitlines():
        if line.rstrip().endswith(":"):
            current = _norm(line.split("(")[0].strip().rstrip(":").strip())
            skill_groups[current] = set()
        elif line.strip() and current:
            skill_groups[current].update(line.split())
    expected = {group: set(flags) for group, flags in CFG["flags"].items()}
    assert skill_groups == expected

    policy_groups = {
        _norm(label.lower()): set(_fenced_after(ROUTING_POLICY_MD, f"**{label}**").split())
        for label in ("Critical-domain", "Elevating", "Operational", "Context")
    }
    assert policy_groups == expected


def test_skill_md_elevating_flag_effect_table_is_complete():
    _, rows = _md_table(SKILL_MD, "What each elevating flag actually does")
    assert {row[0].strip("`") for row in rows} == set(CFG["flags"]["elevating"])


def _predicate_tokens(node) -> set[str]:
    """Every flag, flag-group and dimension name a `when` predicate names."""
    out: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("flag", "any_flag_in"):
                out.add(value)
            elif key == "dimension_at_least":
                out.update(value)
            else:
                out |= _predicate_tokens(value)
    elif isinstance(node, list):
        for item in node:
            out |= _predicate_tokens(item)
    return out


@pytest.mark.parametrize("doc", ["SKILL.md", "routing-policy.md"])
def test_override_lists_are_complete_and_in_config_order(doc):
    text = SKILL_MD if doc == "SKILL.md" else ROUTING_POLICY_MD
    lines = [l for l in _fenced_after(text, "for every task class:").splitlines() if l.strip()]
    overrides = CFG["overrides"]
    assert len(lines) == len(overrides), (
        f"{doc} documents {len(lines)} override rules; the config declares "
        f"{len(overrides)}. A list that claims to be unconditional and is "
        f"short by one is worse than no list.")
    for line, rule in zip(lines, overrides):
        got = _norm(line)
        for token in _predicate_tokens(rule["when"]):
            assert token in got, (doc, rule["name"], token, line)
        effect = rule["effect"]
        if "band_at_least" in effect:
            assert f"max(band, {effect['band_at_least']})" in line, (doc, rule["name"])
        elif "band_exactly" in effect:
            assert f"= {effect['band_exactly']}" in line, (doc, rule["name"])
        else:
            assert effect["route"] in line, (doc, rule["name"])


def test_model_profiles_states_the_long_context_tier_the_config_records():
    """A profile document that quotes only the cheap half of a tiered price
    reads as an unconditional advantage — which is what it did until the
    2026-08-18 audit. The numbers here are the config's."""
    xai = next(m for m in CFG["models"].values() if m["family"] == "xai")
    tier = xai["price_per_mtok"]["long_context"]
    assert f"{tier['above_input_tokens'] // 1000}K" in MODEL_PROFILES_MD
    for value in (tier["input"], tier["output"]):
        assert f"${value:.2f}" in MODEL_PROFILES_MD, value
    assert f"{xai['context_window'] // 1000}K" in MODEL_PROFILES_MD
    sel = CFG["worker_balanced_selection"]
    for flag in sel["prefer_alt_when_flags"]:
        assert flag in MODEL_PROFILES_MD
    assert sel["alt"] in MODEL_PROFILES_MD


def test_review_policy_does_not_outclaim_the_ledger_on_subagent_isolation():
    """C4/C6 (audit 2026-08-18). The ledger records Claude Code subagent
    isolation as `assumed` — documented behaviour, never probed — while this
    document called it "the enforcement boundary" flatly and then singled out
    Codex and grok as the ones carrying an unverified assumption. Two of the
    three natives were hedged and the third, which the default binding actually
    uses, was not."""
    ledger = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
    entry = next(e for item, e in ledger.items() if "Claude Code subagent" in item)
    assert entry["status"] != "verified", "ledger changed; re-read this test"
    text = (SKILL / "references" / "review-policy.md").read_text()
    assert "Subagent context isolation is the\nenforcement boundary." not in text
    assert f"`{entry['status']}`" in text, (
        f"the ledger records this as {entry['status']!r}; the document that "
        f"tells a caller how to enforce isolation must use the same word")


# ---------------------------------------------------------------------------
# Tranche A (design §3 A4) — effort/review 표와 quality-evidence의 계약화.
# 2026-08-19 설계 리뷰가 확인한 drift: effort 표의 유령 행(difficult
# debugging, orchestration)과 model-profiles의 "not established" 서술은
# substring 테스트가 볼 수 없었다.
# ---------------------------------------------------------------------------

EFFORT_ROW_TO_KEYS = {
    "formatting, rename, boilerplate": ["formatting_rename", "boilerplate"],
    "straightforward implementation": ["straightforward_impl"],
    "multi-file feature, debugging, refactoring, architecture, standard review":
        ["multi_file_feature", "debugging", "refactoring", "architecture",
         "standard_review"],
    "multi-system refactoring": ["multi_system_refactoring"],
    "complex architecture, unknown root cause, adversarial review":
        ["complex_architecture", "unknown_root_cause", "adversarial_review"],
}


def test_skill_md_effort_table_matches_effort_by_work():
    """행 라벨→config 키 매핑으로 cell-by-cell 대조. 모든 effort_by_work
    키가 정확히 한 번 커버됨을 함께 assert — 유령 행도 누락 행도 남지
    못한다."""
    text = (SKILL / "SKILL.md").read_text()
    _, rows = _md_table(text, "### Effort")
    covered = []
    for row in rows:
        label = row[0].strip("`")
        keys = EFFORT_ROW_TO_KEYS.get(label)
        assert keys is not None, (
            f"SKILL.md effort row {label!r} maps to no effort_by_work key — "
            f"a ghost row (design §3 A3)")
        for key in keys:
            assert CFG["effort_by_work"][key] == row[1].strip("`"), (label, key)
            covered.append(key)
    assert sorted(covered) == sorted(CFG["effort_by_work"]), (
        "every effort_by_work key must appear exactly once in the table")


def test_skill_md_review_table_matches_config():
    """Step 3 표의 effort/independent를 strict 대조. reviewers 열은
    LOW/HIGH/CRITICAL을 role 리스트로 대조하고, MEDIUM은 산문이므로
    cross-family 토큰만 pin한다 (design §3 A4-2)."""
    text = (SKILL / "SKILL.md").read_text()
    _, rows = _md_table(text, "## Step 3")
    by_band = {row[0].strip("`"): row for row in rows}
    for band in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
        spec = CFG["review"][band]
        row = by_band[band]
        assert row[2].strip("`") == spec["effort"], band
        assert (row[3].strip() == "yes") == spec["independent"], band
        if band != "MEDIUM":
            documented = [r.strip() for r in row[1].replace("`", "").split("+")]
            assert documented == spec["reviewers"], band
    assert "cross-family" in by_band["MEDIUM"][1]


def test_model_profiles_quality_evidence_matches_ledger():
    """두 binding entry가 price_verified_quality_probed인 동안, 문서는 '확립
    안 됨' 서술을 가질 수 없고 head-to-head 수치가 모델 라벨과 같은 문장에
    결합되어야 한다. ledger가 바뀌면 이 테스트를 다시 읽어라."""
    ledger = {e["item"]: e for e in CFG["verification_ledger"]["entries"]}
    for item in ("worker_fast binding: gpt-5.6-luna over claude-haiku-4-5",
                 "worker_balanced binding: grok-4.6 over claude-sonnet-5"):
        assert ledger[item]["status"] == "price_verified_quality_probed", (
            "ledger changed; re-read this test")
    assert "Quality — not established" not in MODEL_PROFILES_MD
    # 수치는 해당 모델 라벨과 같은 문장 안에 결합되어야 한다: 문장 단위로
    # 쪼개 (라벨, 점수) 쌍을 함께 담은 문장이 존재하는지 본다.
    sentences = re.split(r"(?<=[.!?])\s+", MODEL_PROFILES_MD)
    # 라벨은 registry id가 아니어야 한다 — `test_d8_model_ids_appear_only_in_
    # the_registry`가 references/*.md에서 완전한 모델 id를 금지한다 (design §3
    # A2의 "registry key로 지칭" 규율). grok-4.6은 완전한 id라 문서에 쓸 수
    # 없으므로 문서가 이미 쓰는 좌석 라벨로 지칭한다.
    for label, score in (("luna", "436/446"), ("haiku", "442/446"),
                         ("xai frontier", "446/446"), ("sonnet-5", "445/446")):
        assert any(label in s and score in s for s in sentences), (label, score)


# ---------------------------------------------------------------------------
# examples.md is a transcript, so it has to be re-derivable from the router
# ---------------------------------------------------------------------------

def _example_transcripts():
    """(command argv, transcript text) for every routed example in examples.md.

    A fenced block invoking `route_task.py` is always followed by the fenced
    block holding what it printed.
    """
    import shlex
    text = (SKILL / "references" / "examples.md").read_text()
    blocks = re.findall(r"^```\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    pairs = []
    for index, block in enumerate(blocks):
        if "route_task.py" not in block:
            continue
        argv = shlex.split(block.replace("\\\n", " "))
        argv = argv[argv.index([a for a in argv if a.endswith("route_task.py")][0]) + 1:]
        if index + 1 < len(blocks):
            pairs.append((argv, blocks[index + 1]))
    return pairs


def test_examples_md_transcripts_are_what_the_router_actually_emits():
    """examples.md is what an agent reads to PREDICT a route, so a stale
    transcript teaches a binding the router will not produce.

    Round 1 of the issue #19 review found five transcripts still naming the xai
    worker for write classes after the write-seat coupling moved them, and
    nothing caught it: the only existing check on this file is that it contains
    a `$SKILL_DIR` invocation. Registry keys, not model ids — `test_d8` forbids
    ids in `references/*.md`, so the transcripts spell the key and this oracle
    translates before comparing.
    """
    import sys
    sys.path.insert(0, str(SKILL / "scripts"))
    from route_task import Policy, Task, load_config, route  # noqa: E402

    cfg = load_config()
    policy = Policy.of(cfg)
    drifted = []
    for argv, transcript in _example_transcripts():
        emitted = re.search(r"^worker:\s+(\S+)\s+->\s+(\S+)$", transcript, re.MULTILINE)
        if not emitted:
            continue
        if any("<" in token for token in argv):
            # `test_d8` forbids model ids in `references/*.md`, so a few
            # examples spell an id as a `<registry key>` placeholder. Those
            # cannot be replayed; the transcript beside them is still checked
            # by the id-free assertions elsewhere in this file.
            continue
        task = _task_from_argv(argv)
        out = route(task, cfg)
        if out["terminal"] or not out["selected_model"]:
            continue
        actual_key = policy.id_to_key[out["selected_model"]]
        if (emitted.group(1), emitted.group(2)) != (out["selected_role"], actual_key):
            drifted.append(
                f"{' '.join(argv[:4])}: doc says {emitted.group(1)} -> {emitted.group(2)}, "
                f"router emits {out['selected_role']} -> {actual_key}")
    assert not drifted, "examples.md no longer matches the router:\n  " + "\n  ".join(drifted)


def _task_from_argv(argv):
    """The subset of flags examples.md actually uses."""
    import sys
    sys.path.insert(0, str(SKILL / "scripts"))
    from route_task import Task  # noqa: E402

    flags = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--reasoning-centric", "--format"):
            # `--format json` takes a value; the boolean flag does not.
            if token == "--format":
                index += 2
                continue
            flags["reasoning_centric"] = True
            index += 1
            continue
        if index + 1 >= len(argv):
            index += 1
            continue
        value = argv[index + 1]
        key = token[2:].replace("-", "_")
        flags[key] = value
        index += 2
    split = lambda v: [x for x in v.split(",") if x]  # noqa: E731
    return Task(
        task_class=flags["class"],
        complexity=int(flags["complexity"]),
        uncertainty=int(flags["uncertainty"]),
        blast_radius=int(flags["blast_radius"]),
        reversibility=int(flags["reversibility"]),
        reasoning_centric=bool(flags.get("reasoning_centric", False)),
        flags=split(flags.get("flags", "")),
        prior_failures=int(flags.get("prior_failures", 0)),
        prior_models=split(flags.get("prior_models", "")),
        runtime=flags.get("runtime", "claude_code"),
        worker_seat=flags.get("worker_seat"),
        unavailable_roles=split(flags.get("unavailable", "")),
        unavailable_models=split(flags.get("unavailable_models", "")),
    )


def _whole_token(needle: str, text: str) -> bool:
    return re.search(r"(^|[^A-Za-z0-9._-])" + re.escape(needle) + r"([^A-Za-z0-9._-]|$)", text) is not None


def _ledger_blob(entry) -> str:                    # DD-2: item or evidence vouch; argv fields do not [P2-sol-F4]
    return f"{entry.get('item', '')}\n{entry.get('evidence', '')}"


def test_every_verified_model_id_is_named_verbatim_in_a_verified_ledger_row():
    """`claude-fable-5` is a substring of `claude-fable-5-1`; an unanchored
    `in` would let a successor's row vouch for a retired id. Whole tokens only."""
    rows = [e for e in CFG["verification_ledger"]["entries"] if e.get("status") == "verified"]
    blobs = [_ledger_blob(e) for e in rows]
    missing = sorted(m["id"] for m in CFG["models"].values()
                     if m.get("verified") is True and not any(_whole_token(m["id"], b) for b in blobs))
    assert missing == [], f"verified ids with no verbatim ledger row: {missing}"


ATTEMPT_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
CLAUDE_VARIADIC_FLAGS = ("--add-dir", "--allowedTools", "--allowed-tools", "--betas",
                         "--disallowedTools", "--disallowed-tools", "--file",
                         "--mcp-config", "--tools")


def _to_claude_mechanisms():
    for host, entries in CFG["transports"].items():
        spec = entries.get("to_claude")
        if isinstance(spec, dict):
            for key, mech in _mechanisms(spec).items():
                yield host, key, mech


def _direction_rows(host):
    needle = f"transports.{host}.to_claude.mechanism"
    return [e for e in CFG["verification_ledger"]["entries"] if needle in str(e.get("item", ""))]


def _flags_in_order(mech: str, argv: str, where):
    flags = [t for t in mech.split() if t.startswith("-")]
    toks = argv.split()
    positions = [toks.index(f) for f in flags]           # ValueError: a probed argv missing a shipped flag
    assert positions == sorted(positions), (where, flags, argv)


def test_to_claude_recipes_end_with_the_prompt_and_carry_no_variadic_flag():
    """A variadic claude flag swallows a positional prompt that follows it.
    The shipped string carries none; `--mcp-config` is a caller variant."""
    for host, key, mech in _to_claude_mechanisms():
        toks = mech.split()
        assert toks[-1] == '"<prompt>"', (host, key, toks[-1])
        bad = [t for t in toks if t.split("=", 1)[0] in CLAUDE_VARIADIC_FLAGS]
        assert not bad, (host, key, bad)


def test_strict_mcp_token_and_direction_ledger_row_come_together():
    """The token ships only with a verified row that names the direction by
    machine path, quotes the exact probed argv (flags in the same order),
    dates the probe, and lists every probe it rests on."""
    for host, key, mech in _to_claude_mechanisms():
        rows = [r for r in _direction_rows(host) if r.get("status") == "verified"]
        has_token = "--strict-mcp-config" in mech.split()
        assert has_token == bool(rows), (host, has_token, [r.get("item") for r in rows])
        for row in rows:
            assert DATE_RE.match(str(row.get("probed_on", ""))), row.get("item")
            assert str(row.get("cli_version", "")).strip(), row.get("item")
            assert ATTEMPT_ID_RE.match(str(row.get("attempt_id", ""))), row.get("attempt_id")
            _flags_in_order(mech, str(row["argv"]), (host, "argv"))
            probes = row.get("probes") or []
            assert probes, (host, "a direction row must list its probes")
            for probe in probes:
                assert ATTEMPT_ID_RE.match(str(probe.get("attempt_id", ""))), probe
                assert probe.get("outcome") == "SUCCEEDED", probe
                if probe.get("vouches_for_recipe", True):         # a baseline without the token is listed, not vouching [P2-sol-F5]
                    _flags_in_order(mech, str(probe["argv"]), (host, probe["attempt_id"]))


ADAPTERS_SECTION_OF_HOST = {"codex": "### Codex", "grok": "### Codex"}


def _section_fence_tokens(text: str, header: str) -> list[str]:
    """Ordered tokens of the `claude -p` fence inside one adapters.md section,
    with line-continuation backslashes dropped."""
    start = text.index(header)
    nxt = re.search(r"\n### ", text[start + len(header):])
    body = text[start: start + len(header) + (nxt.start() if nxt else len(text))]
    fences = re.findall(r"```bash\n(.*?)```", body, re.S)
    claude = [f for f in fences if f.lstrip().startswith("claude -p")]
    assert len(claude) == 1, (header, len(claude))
    return [t for t in claude[0].replace("\\\n", " ").split() if t != "\\"]


def test_adapters_to_claude_fences_mirror_their_own_direction():
    """Direction-specific: the Codex fence must not be able to vouch for the
    grok string or vice versa once the two may differ."""
    text = (SKILL / "references" / "adapters.md").read_text()
    for host, key, mech in _to_claude_mechanisms():
        assert _section_fence_tokens(text, ADAPTERS_SECTION_OF_HOST[host]) == mech.split(), (host, key)
