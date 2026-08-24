# Contributing

Thanks for looking. A few things are worth knowing before you spend time on a
change.

## This repository is published, not developed in

Andromeda's CLI is developed in a private monorepo alongside the rest of the
product and published here on each release. That has one consequence that
matters to you: **a pull request against this repository cannot be merged
directly.** Accepted changes are applied upstream and arrive here in the next
release, with attribution.

That is not a reason to skip the pull request — it is still the clearest way to
propose something, and the diff is what gets applied. It is a reason not to be
surprised when the merge commit is not yours.

Issues are read and answered here.

## Running it from a checkout

```bash
git clone https://github.com/ZekeVoigt/andromeda-cli.git
cd andromeda-cli
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev,browser]"
.venv/bin/python -m playwright install chromium   # only for the browser tools
.venv/bin/python -m pytest -q
```

Python 3.11–3.13. The upper bound is deliberate and documented in
`pyproject.toml`.

## What a good change looks like

**Tests are not optional, and they assert behaviour rather than shape.** The
suite is large because the things worth pinning here are mostly ordering rules,
refusals and failure modes — the cases where the code is correct and looks
wrong. A test that asserts a function returns a dict teaches nobody anything; a
test that asserts a failing monitor source is treated as an error and never as
a change is the reason that rule survives the next refactor.

**Comments explain why, not what.** Most of the non-obvious decisions in this
codebase carry a paragraph saying what breaks if you do the obvious thing
instead. If you change one of those decisions, change its paragraph. If you
find one that is wrong, that is a valuable issue on its own.

**Dependencies are exact-pinned, and adding one is a real decision.** Every
direct dependency is `==X.Y.Z` with no ranges, because a range lets a fresh
transitive reach users without anyone reviewing it. Provider- and tool-specific
packages belong in `[project.optional-dependencies]` and are installed on first
use. A pull request that adds a core dependency should say what it buys and why
the standard library will not do.

**Tool names and schemas are a contract.** The tools here share names with
another registry, and a test compares the two. If you change a tool's name,
arguments, risk tier or category, that test will tell you — it is not being
difficult, it is telling you the same tool now means two different things
depending on which surface a model meets it on.

## Security

Please do not open a public issue for a vulnerability. `SECURITY.md` has the
address.
