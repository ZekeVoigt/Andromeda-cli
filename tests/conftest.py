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


@pytest.fixture(autouse=True)
def plugin_environment(monkeypatch):
    """Neither plugin switch survives a test.

    Both are read from the process environment rather than passed down, which
    is right for a switch the daemon and `cron run` also have to honour and
    wrong for a suite: one test that turns plugins off turns them off for every
    test after it, and the symptom is a `KeyError` in an unrelated file. That
    is not hypothetical — it happened the first time both were exercised in one
    run.
    """
    from andromeda_agent import plugins as plugins_module

    monkeypatch.delenv(plugins_module.ENV_DISABLE, raising=False)
    monkeypatch.delenv(plugins_module.ENV_PROJECT_PLUGINS, raising=False)


@pytest.fixture(autouse=True)
def wide_console():
    """Stop rich from wrapping console output during tests.

    Rich wraps to the terminal width, and under pytest that width comes from
    the environment — so it differs between a developer's terminal, a CI runner
    and a piped run. A wrapped line puts a newline *inside* a phrase, which
    makes a substring assertion fail on the machine with the narrower terminal
    and pass everywhere else.

    That is not hypothetical. `test_it_refuses_to_overwrite_by_default`
    asserted on "already has" and passed here for weeks; on CI the tmp path is
    longer, the message wrapped as "already \\nhas", and it failed on all three
    Python versions at once — in the published repository, on its first run.

    **The width is set on the console objects, not through `COLUMNS`.** Rich
    reads the environment when a `Console` is constructed, and these are
    module-level, so they already exist by the time any fixture runs. The first
    version of this fixture set `COLUMNS` and did exactly nothing; the suite
    kept passing because this machine's terminal is wide, which is the same
    reason the original bug survived. It was caught by asserting on
    `console.width` rather than by trusting the fixture.

    Fixed wide rather than unlimited: rich treats width as a real number and a
    huge one changes how it lays out tables and rules. 200 is wider than any
    message this program emits and narrow enough to stay realistic.
    """
    from andromeda_cli import render

    consoles = [render.console, render.err_console]
    previous = [console.width for console in consoles]
    for console in consoles:
        console.width = 200
    try:
        yield
    finally:
        for console, width in zip(consoles, previous):
            console.width = width
