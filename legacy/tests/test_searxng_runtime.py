"""A clean checkout must include SearXNG's runtime data package."""

import os
import subprocess
import sys
from pathlib import Path


def test_vendored_searxng_runtime_data_imports():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-c', 'from searx import data; print(data.__name__)'],
        cwd=root,
        env={**os.environ, 'PYTHONPATH': str(root / 'searxng')},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert 'searx.data' in result.stdout
