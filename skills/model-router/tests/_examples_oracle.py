"""examples.md is a transcript of the router. This module is the one parser
the test and the regenerator share (design DD-6 / §4 T8b).

Placeholder grammar: examples.md spells a model id as `<registry_key id>`
(two words, e.g. `<openai_worker_fast id>`). It is collapsed to one token
before shell splitting and substituted from the config before replay; emitted
ids are mapped back to registry keys before comparison (test_d8).

Usage:  python3 tests/_examples_oracle.py --write   # regenerate every transcript
"""
from __future__ import annotations

import io
import re
import shlex
import sys
from contextlib import redirect_stdout
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
EXAMPLES = SKILL / "references" / "examples.md"
NO_TRANSCRIPT = "<!-- no-transcript -->"
FENCE = re.compile(r"^```[A-Za-z0-9_-]*[ \t]*$")
PLACEHOLDER = re.compile(r"<([a-z_]+) id>")


def fenced_blocks(text: str) -> list[tuple[int, int, str]]:
    """(start_line, end_line, body) for every fenced block, language tag or not."""
    lines = text.splitlines()
    blocks, start = [], None
    for i, line in enumerate(lines):
        if FENCE.match(line):
            if start is None:
                start = i
            else:
                blocks.append((start, i, "\n".join(lines[start + 1:i])))
                start = None
    assert start is None, "unterminated fence"
    return blocks


def is_command(body: str) -> bool:
    return "route_task.py" in body


def pairs(text: str):
    """(command argv, transcript block or None, line span). A command block is
    one that invokes route_task.py; its transcript is the very next fenced
    block, unless the NO_TRANSCRIPT marker sits between them. A command block
    followed by another command block has no transcript and needs the marker."""
    blocks = fenced_blocks(text)
    lines = text.splitlines()
    out = []
    for index, (s, e, body) in enumerate(blocks):
        if not is_command(body):
            continue
        argv = shlex.split(PLACEHOLDER.sub(r"<\1>", body.replace("\\\n", " ")))
        argv = argv[argv.index(next(a for a in argv if a.endswith("route_task.py"))) + 1:]
        nxt = blocks[index + 1] if index + 1 < len(blocks) else None
        between = "\n".join(lines[e + 1:nxt[0]]) if nxt else "\n".join(lines[e + 1:])
        if NO_TRANSCRIPT in between:
            out.append((argv, None, None))
            continue
        assert nxt is not None and not is_command(nxt[2]), \
            f"command block at line {s + 1} has no transcript and no {NO_TRANSCRIPT} marker"
        out.append((argv, nxt[2], (nxt[0], nxt[1])))
    return out


VALUED = {"class", "complexity", "uncertainty", "blast_radius", "reversibility", "flags",
          "prior_failures", "prior_models", "runtime", "worker_seat", "unavailable", "unavailable_models",
          "format"}                      # `--format text|json` is valued too [P3-sol-F6]
BOOLEAN = {"reasoning_centric"}
# `--host-model` / `--host-effort` are deliberately NOT modelled: the only command that
# carries them has no transcript (the NO_TRANSCRIPT marker), so a replay would be dead code.


def task_from_argv(argv, cfg):
    """Strict: an option outside VALUED/BOOLEAN, or a valued option with no value,
    raises — a silently dropped option would make the regenerator bless a transcript
    for a different route than the documented command ([P2-opus-F3][P2-opus-F9])."""
    from route_task import Task
    flags, i = {}, 0
    while i < len(argv):
        tok = argv[i]
        key = tok[2:].replace("-", "_") if tok.startswith("--") else None
        if key in BOOLEAN:
            flags[key] = True; i += 1; continue
        if key not in VALUED:
            raise ValueError(f"examples.md command uses an option the oracle does not model: {tok}")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise ValueError(f"examples.md command: {tok} has no value")
        flags[key] = argv[i + 1]; i += 2
    if flags.get("format", "text") not in ("text", "json"):
        raise ValueError(f"examples.md command: --format {flags['format']!r} is not text|json")
    split = lambda v: [x for x in v.split(",") if x]  # noqa: E731
    keys = {k: m["id"] for k, m in cfg["models"].items()}
    sub = lambda v: re.sub(r"<([a-z_]+)>", lambda m: keys[m.group(1)], v)  # noqa: E731
    return Task(
        task_class=flags["class"], complexity=int(flags["complexity"]),
        uncertainty=int(flags["uncertainty"]), blast_radius=int(flags["blast_radius"]),
        reversibility=int(flags["reversibility"]),
        reasoning_centric=bool(flags.get("reasoning_centric", False)),
        flags=split(flags.get("flags", "")), prior_failures=int(flags.get("prior_failures", 0)),
        prior_models=split(sub(flags.get("prior_models", ""))), runtime=flags.get("runtime", "claude_code"),
        worker_seat=flags.get("worker_seat"), unavailable_roles=split(flags.get("unavailable", "")),
        unavailable_models=split(sub(flags.get("unavailable_models", ""))),
    )


def canonical_transcript(argv, cfg) -> str:
    from route_task import Policy, _print_text, route
    out = route(task_from_argv(argv, cfg), cfg)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_text(out)
    text = buf.getvalue()
    policy = Policy.of(cfg)
    for model_id, key in sorted(policy.id_to_key.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(model_id, key)
    return "\n".join(l.rstrip() for l in text.rstrip("\n").splitlines())


def _norm(block: str) -> str:
    return "\n".join(l.rstrip() for l in block.rstrip("\n").splitlines())


def drift(text: str, cfg):
    return [(argv, span) for argv, block, span in pairs(text)
            if block is not None and _norm(block) != canonical_transcript(argv, cfg)]


def rewrite(text: str, cfg) -> str:
    lines = text.splitlines()
    for argv, block, span in reversed(pairs(text)):
        if block is None:
            continue
        s, e = span
        lines[s + 1:e] = canonical_transcript(argv, cfg).splitlines()
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    from route_task import load_config
    cfg = load_config()
    text = EXAMPLES.read_text()
    if "--write" in sys.argv:
        EXAMPLES.write_text(rewrite(text, cfg))
        print("rewritten", sum(1 for _, b, _ in pairs(text) if b is not None), "transcripts")
    else:
        for argv, span in drift(text, cfg):
            print("drift:", " ".join(argv[:4]), span)
