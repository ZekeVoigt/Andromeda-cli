"""The installers the website serves are the installers in this repository.

`curl -fsSL https://ai-andromeda.com/install.sh | bash` is the documented way in,
and what it fetches is a static file under `public/`. That is a second copy of
`install/install.sh`, and a second copy is a thing that goes stale — silently,
and in the worst possible place: the copy people actually run.

The failure this pins is specific. Change `install/install.sh` — fix a repo URL,
add a layout probe — ship it, and every existing user's `andromeda update` picks
it up while every *new* user still runs the stale hosted copy. Nothing breaks
for anyone testing it, because they already have the CLI installed.

So the two are asserted byte-identical. Editing one and not the other fails
here, before it can reach the website.

Skipped outside the monorepo, for the same reason the registry drift guard is:
the published distribution repository does not carry `public/`. The check runs
upstream, and publishing is gated on the suite passing there.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def monorepo_root() -> Path | None:
    """The wider checkout, if the CLI is sitting inside one.

    Identified by `public/` next to the package rather than by directory name,
    because the name is not load-bearing and a checkout can be cloned anywhere.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "public").is_dir() and (parent / "cli" / "install").is_dir():
            return parent
    return None


ROOT = monorepo_root()
pytestmark = pytest.mark.skipif(ROOT is None, reason="running outside the monorepo checkout")


@pytest.mark.parametrize("name", ["install.sh", "install.ps1"])
def test_the_hosted_copy_matches_the_source(name):
    assert ROOT is not None
    source = ROOT / "cli" / "install" / name
    hosted = ROOT / "public" / name

    assert source.is_file(), f"{source} is missing"
    assert hosted.is_file(), (
        f"public/{name} is missing — the website would 404 on the documented "
        f"install command. Copy cli/install/{name} to public/{name}."
    )

    if source.read_bytes() != hosted.read_bytes():
        pytest.fail(
            f"public/{name} has drifted from cli/install/{name}.\n"
            f"The hosted copy is what new users run. Re-copy it:\n"
            f"    cp cli/install/{name} public/{name}"
        )


@pytest.mark.parametrize("name", ["install.sh", "install.ps1"])
def test_the_installer_points_at_the_public_repository(name):
    """A default that only works for someone with private access is not a default.

    The original default here was a placeholder org, which meant the documented
    one-line install failed for everyone who was not already able to clone the
    development monorepo — the exact population an installer exists for.
    """
    assert ROOT is not None
    text = (ROOT / "cli" / "install" / name).read_text(encoding="utf-8")
    assert "github.com/andromeda/andromeda.git" not in text, "placeholder repository URL"
    assert "andromeda-cli.git" in text, "installer does not clone the distribution repository"


def test_the_installer_accepts_both_layouts():
    """The distribution repo is flat; a monorepo checkout is not.

    Both are real install sources, so the installer probes for the package
    marker instead of assuming one shape. Pinned because the failure is
    invisible to whoever changes it: whichever layout they test on works.
    """
    assert ROOT is not None
    text = (ROOT / "cli" / "install" / "install.sh").read_text(encoding="utf-8")
    assert 'if [ -f "$INSTALL_ROOT/cli/pyproject.toml" ]' in text
    assert 'elif [ -f "$INSTALL_ROOT/pyproject.toml" ]' in text
