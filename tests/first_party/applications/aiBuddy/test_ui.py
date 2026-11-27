# file: tests/first_party/applications/aiBuddy/test_ui.py ; version: 1
"""Optional Node suite for the AI Buddy browser UI view model; skipped without Node."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def test_ui_view_model_javascript() -> None:
    node = os.environ.get("TERTALIS_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js 20+ not available (optional; set TERTALIS_NODE to run the AI Buddy UI JS suite).")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("ui.test.mjs"))], cwd=ROOT,
                            text=True, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
