"""Test hermeticity (design §5 "밀폐성"): no test route may read the developer's
local model state. `conftest.py` pins the three variables at import, so every
subprocess a test starts inherits them too."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def test_subprocess_inherits_hermetic_env():
    probe = ("import os;print(os.environ.get('DEEP_MODEL_ROUTER_OVERLAY'), "
             "os.environ.get('DEEP_MODEL_ROUTER_AUTOUPGRADE'), "
             "os.environ.get('DEEP_MODEL_ROUTER_STATE_DIR'))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, check=True).stdout.split()
    assert out[0] == "off"
    assert out[1] == "0"
    state = Path(out[2])
    # A per-session temporary directory, never a developer's real state root.
    assert state.name.startswith("dmr-test-state-"), state
    assert state.resolve().parent == Path(tempfile.gettempdir()).resolve(), state
    assert state.is_dir()
    assert str(state) == os.environ["DEEP_MODEL_ROUTER_STATE_DIR"]
