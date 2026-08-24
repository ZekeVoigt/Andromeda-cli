"""Andromeda's terminal harness."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _installed_version

# Read from the installed package metadata rather than written here.
#
# It was written here, and it drifted immediately: `pyproject.toml` went to
# 0.1.2 while this said 0.1.0, so a freshly installed CLI reported a version
# that had not existed for two releases. That is worse than cosmetic — it is
# the first thing anyone puts in a bug report, and `andromeda doctor` prints
# it, so every report would have named the wrong release.
#
# `pyproject.toml` is the single source now, because it is the one the packaging
# tools and the release tag already agree on.
try:
    __version__ = _installed_version("andromeda-cli")
except PackageNotFoundError:  # pragma: no cover - a source tree with no install
    # Running straight out of a checkout that was never installed. Say so
    # rather than inventing a number that will be wrong.
    __version__ = "0.0.0+source"
