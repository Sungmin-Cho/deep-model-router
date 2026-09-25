"""Hermetic environment for the whole suite (design §5 "밀폐성").

Set at IMPORT, before any test module imports the router, and by assignment
rather than `setdefault`: a value exported in the developer's shell must not
leak into a test route. Subprocesses inherit `os.environ`, so a CLI a test
spawns sees the same three values.

A test that exercises local model state builds its own state directory under
`tmp_path` and passes an explicit environment; nothing may rely on the
session directory below holding anything.
"""
import os
import shutil
import tempfile
import atexit

_STATE_DIR = tempfile.mkdtemp(prefix="dmr-test-state-")
atexit.register(shutil.rmtree, _STATE_DIR, True)

os.environ["DEEP_MODEL_ROUTER_OVERLAY"] = "off"
os.environ["DEEP_MODEL_ROUTER_AUTOUPGRADE"] = "0"
os.environ["DEEP_MODEL_ROUTER_STATE_DIR"] = _STATE_DIR
