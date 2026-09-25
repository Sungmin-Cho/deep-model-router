"""Attended maker-seat probe (design 2026-09-25 DD-A0 "쓰기 좌석", DD-A7).

The automatic path (`model_sync.py tick|run`) never seats a writer: its argv
builder has no write recipe, and it never imports this module. Containment
of a write seat is a property of the TRANSPORT recipe, not of the model id,
so the per-id re-verification of a maker seat is this attended step, run by
a person before `promote`:

* the recipe is the registry's own transport string for the direction
  (`mechanism_maker` where one exists, else `mechanism`), with `-m <id>`
  and the effort filled in. The two write-mode values the strings leave as
  placeholders (`<sandbox>`, `<mode>`) are read from the verification-ledger
  row that verified that direction's child write axis — so the OpenAI seat
  is exactly the ledger's write recipe, and it carries NO receipt guard (a
  guarded codex child with its own `-s` sandbox is refused, DD-B9);
* grok gets the WHOLE `MAKER_SEAT_REQUIRED` supervisor set under
  `--seat-profile grok-maker-v1` — a shortened argv is not a probe, and
  `dispatch_agent.py run` refuses it before spawn;
* claude is certified by the content hash of the file it wrote, never by
  `--require-artifact` (its file tools install a new inode, which that
  contract grades `artifact_identity_replaced`);
* every attempt runs in a disposable, single-linked, empty child cwd.

The summary (argv included) is content-addressed under
`work/probes/makers/`; `promote` turns a passing one into the
`<id> maker seat` ledger row, and its absence into a `maker_not_reprobed`
disclosure row.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatch_agent  # noqa: E402
import model_state  # noqa: E402
import model_sync  # noqa: E402
from model_sync import SyncError  # noqa: E402
from policy_digest import canonical_policy_sha256  # noqa: E402
from secure_io import StateRoot, open_regular  # noqa: E402

RUNTIME_PREFERENCE = ("claude_code", "codex", "grok")
MAKER_DEADLINE_SECONDS = 600
ARTIFACT = "made.txt"
PROMPT = ("Create a file named made.txt in the current working directory whose "
          "entire content is exactly this one line: {token}\n"
          "Do not create or modify any other file. Then reply with exactly: done\n")


def maker_transport(cfg: Mapping, family: str, runtime: str | None = None
                    ) -> tuple[str, str, str, str]:
    """(runtime, transport name, recipe key, recipe string) of the first
    `write_verified` direction to `family`."""
    for rt in ([runtime] if runtime else RUNTIME_PREFERENCE):
        entry = ((cfg.get("transports") or {}).get(rt) or {}).get(f"to_{family}")
        if isinstance(entry, Mapping) and entry.get("write_verified") is True:
            key = "mechanism_maker" if "mechanism_maker" in entry else "mechanism"
            return rt, f"to_{family}", key, entry[key]
    raise SyncError(f"no write_verified transport to {family}"
                    + (f" from {runtime}" if runtime else ""))


def ledger_write_value(cfg: Mapping, runtime: str, family: str, flag: str) -> str:
    """The value the verified child-write-axis ledger row used for `flag`."""
    needle = f"transports.{runtime}.to_{family}.write_verified"
    for e in (cfg.get("verification_ledger") or {}).get("entries") or []:
        if needle in str(e.get("item", "")) and e.get("status") == "verified" \
                and isinstance(e.get("argv"), str):
            tokens = shlex.split(e["argv"])
            if flag in tokens and tokens.index(flag) + 1 < len(tokens):
                return tokens[tokens.index(flag) + 1]
    raise SyncError(f"no verified ledger row for {needle} records {flag}")


def _flag_value(tokens: list[str], flag: str) -> str | None:
    return tokens[tokens.index(flag) + 1] if flag in tokens[:-1] else None


def maker_argv(*, cfg: Mapping, family: str, model_id: str, effort_native: str,
               attempt_id: str, receipt_dir: Path, child_cwd: Path, prompt_file: Path,
               deadline_seconds: float, grok_home: Path | None = None,
               auth_seed: Path | None = None, session_id: str | None = None,
               receipt_guard: bool | None = None, runtime: str | None = None) -> list[str]:
    """`dispatch_agent.py run … -- <the registry's maker recipe>`."""
    rt, name, _, recipe = maker_transport(cfg, family, runtime)
    subs = {"<id>": model_id, "<effort>": effort_native, "<native-effort>": effort_native}
    if "<sandbox>" in recipe:
        subs["<sandbox>"] = ledger_write_value(cfg, rt, family, "-s")
    if "<mode>" in recipe:
        subs["<mode>"] = ledger_write_value(cfg, rt, family, "--permission-mode")
    if family == "xai":
        subs["<fresh-uuid>"] = str(session_id)
        subs["<grok-home>"] = str(grok_home)
    child: list[str] = []
    for tok in shlex.split(recipe):
        if tok == "<prompt>":
            if family == "openai":
                child.append("-")           # the prompt file arrives on stdin
            continue                        # claude -p reads stdin
        for k, v in subs.items():
            tok = tok.replace(k, v)
        child.append(tok)
    if family == "claude":
        child += ["--output-format", "json"]    # the envelope, for served_models
    guard = (sys.platform == "darwin") if receipt_guard is None else receipt_guard
    sup = [sys.executable, str(model_sync.DISPATCH), "run",
           "--attempt-id", attempt_id, "--receipt-dir", str(receipt_dir),
           "--deadline-seconds", str(deadline_seconds), "--grace-seconds", "5",
           "--seat", "worker", "--runtime", model_sync.PROBE_RUNTIME,
           "--model-id", model_id, "--effort-native", effort_native,
           "--transport-id", f"{rt}.{name}",
           "--child-cwd", str(child_cwd), "--prompt-file", str(prompt_file),
           "--output-schema", "none"]
    if family == "claude":
        sup += ["--output-envelope", model_sync.CLAUDE_ENVELOPE,
                "--permission-mode", subs.get("<mode>", "")]
    elif family == "openai":
        sup += ["--output-envelope", model_sync.CODEX_ENVELOPES["text"]]
        guard = False
    elif family == "xai":
        sup += ["--seat-profile", dispatch_agent.MAKER_SEAT_PROFILE,
                "--require-single-linked-cwd",
                "--grok-home", str(grok_home), "--grok-auth-seed", str(auth_seed),
                "--expect-sandbox-enforced", dispatch_agent.MAKER_SANDBOX_PROFILE,
                "--output-envelope", model_sync.GROK_ENVELOPE,
                "--session-evidence",
                f"grok-session-v1:{model_sync.grok_session_dir(grok_home, child_cwd, session_id)}",
                "--session-id", str(session_id),
                "--expect-effective-agent", _flag_value(child, "--agent") or "",
                "--expect-sandbox-profile", dispatch_agent.MAKER_SANDBOX_PROFILE]
    if guard:
        sup += ["--receipt-guard", model_sync.GUARD]
    return [*sup, "--", *child]


def _artifact(child_cwd: Path, token: str) -> dict:
    """The caller-side certification: the content of the file the seat wrote
    (a rename-into-place write passes — no inode is pinned)."""
    try:
        fd, st = open_regular(child_cwd / ARTIFACT)
    except OSError:
        return {"name": ARTIFACT, "present": False, "sha256": None,
                "expected_sha256": hashlib.sha256(token.encode()).hexdigest()}
    with os.fdopen(fd, "rb") as f:
        data = f.read(64 * 1024)
    return {"name": ARTIFACT, "present": True, "nlink": st.st_nlink,
            "raw_sha256": hashlib.sha256(data).hexdigest(),
            "sha256": hashlib.sha256(data.decode("utf-8", "replace").strip().encode()).hexdigest(),
            "expected_sha256": hashlib.sha256(token.encode()).hexdigest()}


def probe_maker(*, key: str, env: Mapping[str, str], home: Path | None,
                now: dt.datetime | None, state_path: Path,
                confirm: Callable[[Mapping], bool], runtime: str | None = None,
                receipt_guard: bool | None = None) -> dict:
    """Plan, ask, run once, certify, record. Returns `{"status": "pass" |
    "failed" | "aborted" | "refused", …}`; "refused" (dispatch_agent exited
    before spawn) writes no summary."""
    from route_task import load_config
    home = Path(home) if home is not None else Path(env.get("HOME") or Path.home())
    now = now or dt.datetime.now(dt.timezone.utc)
    state_path = Path(state_path)
    base = load_config()
    view = model_sync.committed_view(state_path, base)
    row = view.config["models"].get(key)
    if not isinstance(row, Mapping) or "lineage" not in row or "history_of" in row:
        raise SyncError(f"{key!r} is not a live lineage row")
    family, model_id = row["family"], row["id"]
    rt, name, recipe_key, _ = maker_transport(base, family, runtime)
    effort = model_sync.native_token(base, row, "LOW")
    run_id = f"m{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    receipt_dir = state_path / model_sync.RECEIPTS_RELPATH / run_id
    auth_seed = home / ".grok" / "auth.json"
    lines = [f"key: {key}  family: {family}  id: {model_id}",
             f"recipe: transports.{rt}.{name}.{recipe_key}",
             "child cwd: a new, empty, single-linked temporary directory (removed after)",
             f"receipts: {receipt_dir}",
             f"summary: {state_path / model_sync.MAKERS_PREFIX}/<sha256>.json",
             f"state: {state_path / model_sync.WORK_STATE} (makers.{key})"]
    if family == "xai":
        lines.append(f"grok auth seed copied onto a new inode from: {auth_seed}")
    plan = {"key": key, "family": family, "model_id": model_id,
            "transport_id": f"{rt}.{name}", "lines": lines}
    if not confirm(plan):
        return {"status": "aborted", "plan": plan}
    if family == "xai" and not auth_seed.is_file():
        raise SyncError(f"grok maker seat needs an auth seed at {auth_seed}")

    with StateRoot.open(state_path, create=True) as root:
        root.mkdir(f"{model_sync.RECEIPTS_RELPATH}/{run_id}")
    scratch = Path(tempfile.mkdtemp(prefix="dmr-maker-"))
    try:
        child_cwd = model_sync.new_child_cwd(scratch / "work")
        prompt_dir = Path(tempfile.mkdtemp(prefix="prompt-", dir=scratch))
        token = f"MADE-{secrets.token_hex(8)}"
        prompt = prompt_dir / "prompt.txt"
        prompt.write_text(PROMPT.format(token=token))
        grok_home = scratch / "grok-home" if family == "xai" else None
        session_id = str(uuid.uuid4())
        attempt = f"{run_id}-1"
        argv = maker_argv(cfg=base, family=family, model_id=model_id, effort_native=effort,
                          attempt_id=attempt, receipt_dir=receipt_dir, child_cwd=child_cwd,
                          prompt_file=prompt, deadline_seconds=MAKER_DEADLINE_SECONDS,
                          grok_home=grok_home, auth_seed=auth_seed, session_id=session_id,
                          receipt_guard=receipt_guard, runtime=runtime)
        try:
            proc = subprocess.run(argv, env=dict(env), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=MAKER_DEADLINE_SECONDS + 120)
            code, stderr = proc.returncode, proc.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            code, stderr = None, str(exc)
        receipt, receipt_sha = model_sync._read_evidence_json(receipt_dir / f"{attempt}.json")
        if receipt is None:
            return {"status": "refused", "exit_code": code, "argv": argv,
                    "stderr": (stderr or "")[-4000:]}
        result = receipt.get("result") or {}
        artifact = _artifact(child_cwd, token)
        ok = model_sync._succeeded(receipt) and artifact["present"] \
            and artifact.get("nlink") == 1 and artifact["sha256"] == artifact["expected_sha256"]
        env_rec = result.get("envelope") or {}
        cli = model_sync.FAMILY_CLI[family]
        summary: dict[str, Any] = {
            "kind": "maker", "attended": True, "key": key, "family": family, "id": model_id,
            "transport_id": f"{rt}.{name}", "recipe_key": recipe_key, "argv": argv,
            "attempt_id": attempt, "receipt_sha256": receipt_sha,
            "state": result.get("state"),
            "termination_confirmed": result.get("termination_confirmed"),
            "exit_code": code, "served_models": list(env_rec.get("served_models") or []),
            "header_model": env_rec.get("header_model"),
            "artifact": artifact, "receipt_guard": "--receipt-guard" in argv,
            "outcome": "pass" if ok else "failed",
            "reason": None if ok else ("artifact" if model_sync._succeeded(receipt)
                                       else "attempt"),
            "cli_version": model_sync.cli_versions(env).get(cli),
            "base_policy_sha256": canonical_policy_sha256(base),
            "date": now.date().isoformat()}
        with StateRoot.open(state_path) as root:
            sha = model_state.write_addressed(root, model_sync.MAKERS_PREFIX, summary)

        def note(st):
            st["makers"][key] = {"id": model_id, "summary_sha256": sha,
                                 "outcome": summary["outcome"], "date": summary["date"]}
        model_sync.update_work_state(state_path, note)
        return {"status": summary["outcome"], "summary": summary, "summary_sha256": sha}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
