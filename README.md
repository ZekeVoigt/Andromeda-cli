<div align="center">
  <img src=".github/assets/andromeda-mark.svg" width="88" alt="Andromeda mark">
  <h1>Andromeda CLI</h1>
  <p><strong>A local-first AI agent built for the terminal.</strong></p>
  <p>Work across files, shells, the web, and recurring jobs—without giving up control of your machine.</p>

  <p>
    <a href="https://github.com/ZekeVoigt/andromeda-cli/actions/workflows/ci.yml"><img src="https://github.com/ZekeVoigt/andromeda-cli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%E2%80%933.13-18181b?logo=python&logoColor=white" alt="Python 3.11 through 3.13"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-18181b.svg" alt="MIT License"></a>
    <a href="https://ai-andromeda.com"><img src="https://img.shields.io/badge/Website-ai--andromeda.com-7c3aed" alt="Andromeda website"></a>
  </p>
</div>

Andromeda is an agent harness that runs its control loop locally. Ask it to
understand a codebase, edit files, run commands, research the web, or monitor a
recurring task. It works interactively, as a one-shot command, or in a
full-screen terminal interface.

```console
$ andromeda "find the failing test, explain the cause, and fix it"
$ git log -5 | andromeda "summarise these changes"
$ andromeda --tui
```

## Why Andromeda

- **Local-first execution.** The agent loop, tools, sessions, memory, and
  configuration live on your machine.
- **Consent by default.** File writes, shell commands, browser actions, and
  third-party tools stop for approval in the default mode.
- **Useful beyond chat.** Built-in tools cover code, background processes,
  browser workflows, MCP servers, skills, memory, delegation, and schedules.
- **Interactive or scriptable.** Use the REPL, the full-screen TUI, a single
  prompt, or stdin from another command.
- **Choose your provider.** Use Andromeda's hosted relay or bring an API key for
  an OpenAI-compatible endpoint.

## Install

macOS, Linux, and WSL:

```bash
curl -fsSL https://ai-andromeda.com/install.sh | bash
```

Windows PowerShell:

```powershell
iex (irm https://ai-andromeda.com/install.ps1)
```

The installer uses `uv`, creates an isolated environment under
`~/.andromeda-cli/`, and adds the `andromeda` command. It runs a short guided
setup when a terminal is available.

The CLI supports Python 3.11–3.13. The installer requires Git and bootstraps
`uv` when it is not already available.

### Install from source

```bash
git clone https://github.com/ZekeVoigt/andromeda-cli.git
cd andromeda-cli
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/andromeda
```

## Quick start

Run `andromeda` to open the REPL, or choose one of the other entry points:

```bash
andromeda                              # interactive REPL
andromeda --tui                        # full-screen interface
andromeda "explain this repository"    # one turn, then exit
andromeda --workspace ~/code/my-app    # set the file-tool boundary
andromeda doctor                       # verify the installation
```

### Connect a provider

The default `relay` provider uses an Andromeda account and keeps provider keys
out of the CLI:

```bash
andromeda auth login
andromeda auth status
```

The command opens a secure browser sign-in and pairs this machine automatically.
For SSH sessions or machines without a local browser, generate a short-lived
code from your account page and run `andromeda auth login <pairing-code>`.

To use your own OpenAI-compatible provider instead:

```bash
export OPENROUTER_API_KEY="..."
andromeda config set provider direct
andromeda
```

The direct endpoint and key environment variable are configurable.

## What it can do

| Capability | Included |
|---|---|
| Workspace | Read, search, write, and patch files inside a resolved workspace boundary |
| Terminal | Run foreground commands and manage long-running background processes |
| Web | Fetch pages, search with Brave or Tavily, and optionally drive Chromium |
| Integrations | Connect stdio or streamable HTTP MCP servers |
| Context | Persist sessions, rewind checkpoints, load skills, and recall local memory |
| Delegation | Run narrowed scout, browser, writer, and verifier lanes concurrently |
| Automation | Schedule agent tasks, monitors, scripts, chained jobs, and notifications |
| Portability | Back up, export, restore, update, and diagnose an installation |

The browser is optional and installed on demand:

```bash
andromeda browser install
andromeda browser status
```

MCP configuration lives at `~/.andromeda-cli/mcp.json`:

```bash
andromeda mcp              # list configured servers and tools
andromeda mcp example      # print a starter configuration
```

See the [CLI reference](docs/CLI_REFERENCE.md) for commands, configuration,
paths, exit codes, and automation examples.

## Safety model

Andromeda is powerful because it can act on a real machine. The default
approval mode is therefore `ask`:

| Mode | Behavior |
|---|---|
| `ask` | Safe local reads run; actions that can change state pause for approval |
| `deny` | Gated tools are refused |
| `auto` | Gated tools run without prompting |

```bash
andromeda --approval deny "review this repository"
andromeda --approval ask
andromeda --approval auto "apply the migration"
```

File tools are confined to the workspace after resolving symlinks. The shell
is intentionally **not** sandboxed: approving a terminal command gives it the
same reach as running that command yourself. Non-interactive sessions remove
tools that would need a prompt unless you explicitly use `--approval auto`.

Read [SECURITY.md](SECURITY.md) for the complete trust model and private
reporting instructions.

## Automation

Schedule a recurring task, a one-time follow-up, or a cheap monitor that only
calls the model when its source changes:

```bash
andromeda cron add "every 1h" "check the build and report failures"
andromeda cron add "in 2h" "check whether the deploy finished"
andromeda cron add "every 10m" "summarise any change" --watch status.sh
andromeda cron install
andromeda cron service
```

Approval is fixed when a job is created, and a scheduled job cannot widen the
permissions of the session that created it. Every attempt is recorded,
including failures and interrupted runs.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev,browser]"
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q
```

CI runs the suite on Python 3.11, 3.12, and 3.13, with an additional macOS
job for platform-specific terminal and process behavior.

The main packages are deliberately separated by responsibility:

```text
andromeda_cli/     command parsing, REPL, configuration, sessions, rendering
andromeda_agent/   model loop, providers, approvals, delegation, automation
andromeda_tools/   tool registry and executors
andromeda_tui/     full-screen terminal interface
```

## Project links

- [CLI reference](docs/CLI_REFERENCE.md)
- [Changelog](CHANGELOG.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Andromeda website](https://ai-andromeda.com)

This repository is the public release mirror for the CLI. Development happens
with the wider Andromeda product upstream; issues and pull requests are still
welcome here, and accepted changes are carried upstream with attribution.

## License

[MIT](LICENSE) © 2026 Zeke Voigt.
