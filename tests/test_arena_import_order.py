"""Arena and Pi adapter imports must not depend on which module loads first."""

import subprocess
import sys


def test_pi_adapter_then_arena_runner_imports_without_cycle():
    code = (
        "from skharness.autocode.adapters.pi import PiAdapter; "
        "from skharness.arena.runner import PiExperimentRunner; "
        "assert PiAdapter and PiExperimentRunner"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_arena_package_then_pi_adapter_imports_without_cycle():
    code = (
        "import skharness.arena; "
        "from skharness.autocode.adapters.pi import PiAdapter; "
        "assert PiAdapter"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
