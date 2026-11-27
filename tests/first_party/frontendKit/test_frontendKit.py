# file: tests/first_party/frontendKit/test_frontendKit.py ; version: 1
"""Tertalis-suite entry point for the frontend kit's deterministic Node tests."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_frontend_kit_javascript_contracts() -> None:
    """Runs the kit's pure transport/session suite under Node without browser dependencies."""
    node = os.environ.get("TERTALIS_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js 20+ not available (optional; set TERTALIS_NODE to run the frontend kit JS suite).")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("kit.test.mjs"))], cwd=ROOT,
                            text=True, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
