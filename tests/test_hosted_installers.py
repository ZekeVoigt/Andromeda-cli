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


@pytest.mark.parametrize("name,pattern", [
    ("install.sh", "uv venv --clear"),
    ("install.ps1", "uv venv --clear"),
])
def test_the_installer_can_be_re_run(name, pattern):
    """`uv venv` refuses an existing directory, so this must pass `--clear`.

    Without it the installer works exactly once: every re-run dies at "Could
    not create the venv". That is not a rare path — re-running the installer is
    what this script's own dependency-failure message tells people to do, and
    it is how anyone recovers a broken install or moves to a build whose
    `update` is broken.
    """
    assert ROOT is not None
    text = (ROOT / "cli" / "install" / name).read_text(encoding="utf-8")
    assert pattern in text, f"{name} cannot be run twice"


def test_the_hosted_plugin_index_matches_the_bundled_seed():
    """Same failure as the installers, one directory over.

    `plugins search` fetches `https://ai-andromeda.com/plugins/index.json`, and
    what serves it is a static file under `public/`. The bundled seed beside the
    package is the offline fallback *and* the format reference, so the two are
    two copies of one document — and a second copy is a thing that goes stale
    in the worst place: the one people actually fetch.

    The `install.sh` URL 404'd once for the neighbouring version of this
    mistake. This fails before the next one can reach the website.
    """
    root = monorepo_root()
    if root is None:
        pytest.skip("not inside the monorepo")

    from andromeda_agent import plugin_index

    hosted = root / "public" / "plugins" / "index.json"
    assert hosted.is_file(), f"{hosted} is missing — `plugins search` would 404"
    assert hosted.read_bytes() == plugin_index.seed_path().read_bytes(), (
        f"{hosted} and {plugin_index.seed_path()} have diverged. Copy one over "
        f"the other; they are two copies of one document."
    )


def test_the_hosted_index_is_what_the_client_fetches():
    """The path in the file and the path in the code are the same path."""
    root = monorepo_root()
    if root is None:
        pytest.skip("not inside the monorepo")

    from andromeda_agent import plugin_index

    served = (root / "public").joinpath(
        *plugin_index.DEFAULT_INDEX_URL.split("://", 1)[1].split("/")[1:]
    )
    assert served.is_file(), (
        f"{plugin_index.DEFAULT_INDEX_URL} would resolve to {served}, which "
        f"does not exist"
    )
