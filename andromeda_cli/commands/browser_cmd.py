"""Installing and inspecting the browser dependency."""

from __future__ import annotations

import subprocess
import sys

from andromeda_tools import browser as browser_module

from .. import output

INSTALL_TIMEOUT = 900


def status() -> int:
    if not browser_module.playwright_available():
        output.info("Playwright is not installed — the browser tools are off.")
        output.info("  andromeda browser install")
        return 1

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as driver:
            path = driver.chromium.executable_path
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        output.fail(f"Playwright is installed but Chromium is not usable: {exc}")
        output.info("  andromeda browser install")
        return 1

    output.ok("Browser tools are available.")
    output.console.print(f"  [dim]{path}[/dim]", soft_wrap=True)
    return 0


def install() -> int:
    """Install Playwright and its Chromium, into this install's own venv.

    `sys.executable` rather than a bare `pip`: the CLI may be reached through a
    symlink from `~/.local/bin`, and installing into whatever `pip` happens to
    be first on PATH is how a dependency lands somewhere the running
    interpreter cannot see it.
    """
    if not browser_module.playwright_available():
        output.info("Installing Playwright…")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright==1.58.0"],
            timeout=INSTALL_TIMEOUT,
        )
        if result.returncode != 0:
            output.fail("Could not install Playwright.")
            return 1

    output.info("Installing Chromium (skipped if already present)…")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        timeout=INSTALL_TIMEOUT,
    )
    if result.returncode != 0:
        output.fail("Could not install Chromium.")
        return 1

    output.ok("Browser tools are ready. Start a new session to use them.")
    return 0
