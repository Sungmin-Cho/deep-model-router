"""Plugin packaging layout and public-doc contract."""
import json
import re
from pathlib import Path


PUBLIC_FILES = (
    "README.md",
    "README.ko.md",
    "CHANGELOG.md",
    "CHANGELOG.ko.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "SECURITY.md",
    "package.json",
    "docs/locator.md",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            return parent
    raise AssertionError("no .claude-plugin/plugin.json above " + str(here))


def _read_json(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def test_scripts_can_see_plugin_manifest():
    root = _repo_root()
    data = _read_json(root, ".claude-plugin/plugin.json")
    assert data["name"] == "deep-model-router"
    assert isinstance(data["version"], str) and data["version"]


def test_skill_lives_under_skills_model_router():
    root = _repo_root()
    assert (root / "skills" / "model-router" / "SKILL.md").is_file()
    assert (root / "skills" / "model-router" / "scripts" / "route_task.py").is_file()


def test_public_plugin_surface_exists():
    root = _repo_root()
    missing = [rel for rel in PUBLIC_FILES if not (root / rel).is_file()]
    assert missing == [], missing


def test_version_triple_sync():
    root = _repo_root()
    versions = {
        rel: _read_json(root, rel)["version"]
        for rel in (
            "package.json",
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
        )
    }
    assert len(set(versions.values())) == 1, versions


def test_readme_language_toggles():
    root = _repo_root()
    en = (root / "README.md").read_text(encoding="utf-8").splitlines()[0]
    ko = (root / "README.ko.md").read_text(encoding="utf-8").splitlines()[0]
    assert en == "**English** | [한국어](./README.ko.md)"
    assert ko == "[English](./README.md) | **한국어**"


def test_changelog_records_current_version():
    root = _repo_root()
    version = _read_json(root, ".claude-plugin/plugin.json")["version"]
    heading = re.compile(rf"^## \[{re.escape(version)}\] — \d{{4}}-\d{{2}}-\d{{2}}", re.M)
    for rel in ("CHANGELOG.md", "CHANGELOG.ko.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert heading.search(text), rel


def test_agent_guides_do_not_hardcode_version():
    root = _repo_root()
    version = _read_json(root, ".claude-plugin/plugin.json")["version"]
    for rel in ("AGENTS.md", "CLAUDE.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert version not in text, rel
        assert "docs/DOCS_RULE.md" in text, rel


def test_agents_points_at_existing_locator():
    root = _repo_root()
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "docs/locator.md" in text
    assert "once it exists" not in text


# ---------------------------------------------------------------------------
# B13 / B14 (audit 2026-08-18) — the README's two runtime claims
# ---------------------------------------------------------------------------

def _config():
    import sys
    root = _repo_root()
    sys.path.insert(0, str(root / "skills" / "model-router" / "scripts"))
    from route_task import load_config
    return load_config()


def test_readme_states_the_pyyaml_requirement():
    """The README promised "Python 3 required ... no Node runtime dependency"
    and never named PyYAML, which `load_config` and `policy_digest` both
    import. There is no requirements.txt to discover it from either, so the
    first route on a PyYAML-less box failed with an ImportError the install
    instructions gave no warning about."""
    root = _repo_root()
    for rel in ("README.md", "README.ko.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "PyYAML" in text, rel
        assert "pip install pyyaml" in text, rel


def test_readme_does_not_document_the_human_gate_status_as_fixed():
    """Exit 3 is `human_in_the_loop.human_gate_exit_status`, configurable over
    3..255. A caller that hard-codes 3 because the README presented it as fixed
    reads a gate it may have moved."""
    root = _repo_root()
    key = "human_gate_exit_status"
    assert isinstance(_config()["human_in_the_loop"][key], int)
    for rel in ("README.md", "README.ko.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert key in text, rel
        assert "3..255" in text, rel


def test_codex_agent_interface_sidecar_is_well_formed():
    """`skills/model-router/agents/openai.yaml` is referenced by nothing in
    this repo (audit 2026-08-18 §5) because its consumer is the Codex host, not
    this code. That makes it exactly the kind of file that rots unnoticed, so
    the shape it must keep is pinned here, and its display name is held to the
    same product name the Codex plugin manifest declares."""
    import yaml
    root = _repo_root()
    path = root / "skills" / "model-router" / "agents" / "openai.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    interface = spec["interface"]
    assert set(interface) == {"display_name", "short_description", "default_prompt"}
    assert all(isinstance(v, str) and v.strip() for v in interface.values())
    manifest = _read_json(root, ".codex-plugin/plugin.json")["interface"]
    assert interface["display_name"] in manifest["longDescription"] or \
        interface["display_name"] in manifest["displayName"]


def test_codex_manifest_and_sidecar_name_both_axes():
    root = _repo_root()
    manifest = _read_json(root, ".codex-plugin/plugin.json")["interface"]
    for field in ("shortDescription", "longDescription"):
        assert "difficulty" in manifest[field] and "risk" in manifest[field], field
    import yaml
    spec = yaml.safe_load((root / "skills" / "model-router" / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    assert "difficulty" in spec["interface"]["short_description"] and "risk" in spec["interface"]["short_description"]
