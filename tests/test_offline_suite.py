"""The default `pytest` run must be offline and must not need the embedding model."""

import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def collect(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *extra],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )


def test_default_run_deselects_every_real_model_test():
    result = collect()
    assert result.returncode == 0, result.stderr
    for module in ("test_semantic_model", "test_calibration", "test_live_anthropic"):
        assert module not in result.stdout
    assert "deselected" in result.stdout


def test_model_tests_still_exist_and_are_selectable_explicitly():
    result = collect("-m", "model")
    assert result.returncode == 0, result.stderr
    assert "test_semantic_model.py::" in result.stdout


def test_live_api_tests_exist_but_only_run_when_asked():
    result = collect("-m", "live")
    assert result.returncode == 0, result.stderr
    assert "test_live_anthropic.py::" in result.stdout


def test_markers_are_registered():
    result = collect("--markers")
    assert result.returncode == 0
    assert "@pytest.mark.model" in result.stdout and "@pytest.mark.live" in result.stdout


def test_network_access_is_refused_in_offline_tests():
    with pytest.raises(RuntimeError, match="offline test"):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(RuntimeError, match="offline test"):
        socket.getaddrinfo("example.com", 80)


def test_offline_fixtures_do_not_touch_the_model_cache(settings, manager, tmp_path):
    manager.create_collection("c")
    assert not settings.model_cache_dir.exists()  # no download was attempted
