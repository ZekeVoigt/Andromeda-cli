from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ANDROMEDA_HOME.

    Without this the suite reads and writes the developer's real credentials.
    """
    monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path / "home"))
