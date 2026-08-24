#!/usr/bin/env bash
# ============================================================================
# Andromeda CLI installer
# ============================================================================
#   curl -fsSL https://ai-andromeda.com/install.sh | bash
#
# Deliberately not pip and not Homebrew. The CLI resolves bundled assets
# (skills, prompts) from its checkout at runtime, so a wheel would install a
# binary that cannot find half of itself. Cloning is the distribution.
#
# Layout:
#   per-user   ~/.andromeda-cli/checkout/   binary -> ~/.local/bin/andromeda
#   root       /usr/local/lib/andromeda-cli binary -> /usr/local/bin/andromeda
#
# Deliberately not under ~/.andromeda: that is the desktop app's data
# directory, and two programs sharing one root is how clearing the state of
# one silently destroys the other.
# ============================================================================
set -euo pipefail

# The public distribution repository. It carries the CLI and nothing else, so
# a clone is small and no part of the wider product is published to install it.
# ANDROMEDA_REPO_URL points this at a monorepo checkout for internal testing;
# the layout probe below handles both shapes.
REPO_URL="${ANDROMEDA_REPO_URL:-https://github.com/ZekeVoigt/andromeda-cli.git}"
BRANCH="${ANDROMEDA_BRANCH:-main}"

GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; CYAN=$'\033[0;36m'
RED=$'\033[0;31m'; NC=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { printf '%s→%s %s\n' "$CYAN" "$NC" "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$NC" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$NC" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  INSTALL_ROOT="/usr/local/lib/andromeda-cli"
  BIN_DIR="/usr/local/bin"
else
  INSTALL_ROOT="${ANDROMEDA_HOME:-$HOME/.andromeda-cli}/checkout"
  BIN_DIR="$HOME/.local/bin"
fi

# Stop uv from discovering a uv.toml or pyproject.toml belonging to another
# user's home when this runs under `sudo -u`.
export UV_NO_CONFIG=1

say ""
printf '%s⬡ Andromeda CLI%s\n' "$CYAN" "$NC"
say ""

command -v git >/dev/null 2>&1 || die "git is required. Install it and re-run."

if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (Python toolchain manager)"
  curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
    || die "Could not install uv. See https://astral.sh/uv"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv installed but is not on PATH. Open a new shell and re-run."
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

if [ -d "$INSTALL_ROOT/.git" ]; then
  step "Updating existing install at $INSTALL_ROOT"
  git -C "$INSTALL_ROOT" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1 \
    || die "Could not fetch $BRANCH."
  git -C "$INSTALL_ROOT" reset --hard "origin/$BRANCH" >/dev/null 2>&1 \
    || die "Could not update the checkout."
else
  step "Cloning into $INSTALL_ROOT"
  mkdir -p "$(dirname "$INSTALL_ROOT")"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_ROOT" >/dev/null 2>&1 \
    || die "Could not clone $REPO_URL."
fi

# Two layouts are real: the distribution repo, which *is* the package, and a
# monorepo checkout, where the package is one directory of it. Probe for the
# marker rather than assume, so ANDROMEDA_REPO_URL can point at either.
if [ -f "$INSTALL_ROOT/cli/pyproject.toml" ]; then
  CLI_DIR="$INSTALL_ROOT/cli"
elif [ -f "$INSTALL_ROOT/pyproject.toml" ]; then
  CLI_DIR="$INSTALL_ROOT"
else
  die "Checkout has no pyproject.toml — wrong repository or branch?"
fi

# The venv install is the step that can leave a half-working tree: git has
# already moved to the new revision, so a failure here means new code against
# old dependencies. Fail loudly rather than leaving an unbootable `andromeda`.
step "Building the environment"
# `--clear` is required, not tidiness. `uv venv` refuses outright when the
# directory already exists, so without it the installer works exactly once: a
# re-run dies at "Could not create the venv" — and re-running the installer is
# what this script's own dependency-failure message tells people to do. It also
# guarantees the venv matches the interpreter this run selected, rather than
# inheriting whatever an older install built.
uv venv --clear --python 3.13 "$CLI_DIR/.venv" >/dev/null 2>&1 \
  || die "Could not create the venv at $CLI_DIR/.venv."
uv pip install --python "$CLI_DIR/.venv/bin/python" -e "$CLI_DIR" >/dev/null 2>&1 \
  || die "Dependency install failed. The checkout is updated but not runnable — re-run this installer."
ok "Environment ready"

mkdir -p "$BIN_DIR"
ln -sf "$CLI_DIR/.venv/bin/andromeda" "$BIN_DIR/andromeda"
ok "Linked $BIN_DIR/andromeda"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add it:"
     say  "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac

say ""
ok "Installed."

# ---------------------------------------------------------------------------
# Setup runs here, not on first launch
# ---------------------------------------------------------------------------
# Somebody who has just watched an install finish is already paying attention;
# asking then costs nothing. Asking on first launch interrupts the moment they
# finally have a prompt and something to type into it.
#
# `< /dev/tty` is the whole trick. This script is being read by bash *from a
# pipe* — `curl … | bash` — so the script's stdin is its own source. A child
# that reads stdin gets the remaining bytes of this file as the user's
# answers. Redirecting from /dev/tty hands it the real terminal instead.
#
# Guarded, because there is not always a terminal: piping the installer inside
# CI or a Dockerfile is legitimate and must not hang on a read that never
# returns. The CLI checks again on its own side; this is belt and braces on the
# one failure that would look like a frozen install.
if [ -e /dev/tty ] && [ -r /dev/tty ]; then
  say ""
  "$CLI_DIR/.venv/bin/andromeda" setup < /dev/tty || true
else
  say ""
  say "  No terminal available, so setup was skipped."
  say "  Run it when you have one:   andromeda setup"
  say ""
fi
