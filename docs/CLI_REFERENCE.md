# Andromeda CLI reference

This is the compact command and configuration reference. For installation and
a product overview, start with the [README](../README.md).

## Entry points

```text
andromeda                         interactive REPL
andromeda --tui                   full-screen interface
andromeda "prompt"                one non-interactive turn
command | andromeda "prompt"      include stdin in the prompt
```

### Global options

| Option | Purpose |
|---|---|
| `--model ID` | Override the configured model for one invocation |
| `--provider relay\|direct` | Override the provider for one invocation |
| `--thinking off\|low\|medium\|high` | Set reasoning effort |
| `--approval auto\|ask\|deny` | Override the approval mode |
| `--workspace PATH` | Set the file-tool boundary; defaults to the current directory |
| `--tui` / `--no-tui` | Select the terminal interface for one session |
| `--resume ID` | Resume a saved session; unambiguous ID prefixes work |
| `--continue` | Resume the most recent session |
| `--version` | Print the installed version |

`--tui` requires a terminal on stdin and stdout. It cannot be combined with a
one-shot prompt or a pipe.

## Authentication and providers

### Hosted relay

```bash
andromeda auth login
andromeda auth login <pairing-code>  # headless/SSH fallback
andromeda auth status
andromeda auth logout
```

The relay holds the provider key and settles usage. Pairing stores a device
token in `~/.andromeda-cli/credentials.json` with mode `0600` on Unix.

### Direct provider

The direct provider sends requests to an OpenAI-compatible endpoint. Its
defaults are OpenRouter and the `OPENROUTER_API_KEY` environment variable.

```bash
export OPENROUTER_API_KEY="..."
andromeda config set provider direct
andromeda config set direct_base_url https://openrouter.ai/api/v1
andromeda config set direct_api_key_env OPENROUTER_API_KEY
```

## Tools and approvals

```bash
andromeda tools
andromeda tools enable <name>
andromeda tools disable <name>
andromeda approvals
andromeda approvals forget <tool>
andromeda approvals clear
```

In `ask` mode, local reads can run immediately while writes, terminal commands,
browser actions, and third-party MCP tools pause for approval. At a prompt,
approve once, approve that tool for the session, or deny it. An interrupted
approval is a denial.

In a non-interactive run there is nobody to answer an approval prompt, so gated
tools are not offered. `--approval auto` is the explicit way to make them
available to a script.

File tools remain inside the resolved workspace. The terminal tool runs a real,
unsandboxed shell command.

## Sessions and checkpoints

```bash
andromeda sessions
andromeda sessions show <id>
andromeda sessions search <text>
andromeda --resume <id>
andromeda --continue
```

Inside an interactive session:

```text
/history        list checkpoints
/rewind         discard the latest exchange
/rewind 3       return to checkpoint 3
```

Sessions are written after each exchange rather than only at clean exit.

## Browser

```bash
andromeda browser install
andromeda browser status
```

Browser support is optional. Until Playwright and Chromium are installed, the
`browser_*` tools are not registered.

The browser exposes a structured outline with stable element references rather
than screenshots. Private, loopback, and link-local addresses are blocked by
default, including after redirects. To work with a local development server:

```bash
andromeda config set allow_private_network true
```

This setting widens network access for the session; use it deliberately.

## MCP servers

```bash
andromeda mcp
andromeda mcp example
```

Configuration lives in `~/.andromeda-cli/mcp.json`. Both `mcpServers` and
`mcp_servers` are accepted. Stdio and streamable HTTP transports are supported.
Tools are registered as `mcp__<server>__<tool>` and treated as outbound actions.

## Background processes

The terminal tool can start a process in the background and returns a session
ID. The `process` tool manages it with these actions:

```text
list  poll  log  wait  kill  write  submit  close
```

IDs accept an unambiguous prefix. Background process output is drained from
startup to avoid pipe-buffer deadlocks, and remaining processes are stopped
when the interactive session ends.

## Skills and memory

Skills use the `skills/<name>/SKILL.md` format. Discovery checks:

1. `ANDROMEDA_BUNDLED_SKILLS_DIR`
2. `skills/` while walking up from the workspace
3. `~/.andromeda-cli/skills/`

Only skill names and descriptions enter the initial prompt; full instructions
load on demand. `/skills` lists what is available.

Local memory uses `memory_store`, `memory_search`, and `memory_forget`, backed
by `~/.andromeda-cli/memory/`. Recall in the CLI is lexical rather than
embedding-based.

## Delegation

`delegate` starts a narrowed helper lane and returns its ID. Up to three lanes
run concurrently; `subagents_wait` collects their reports.

| Lane | Intended access | Step limit |
|---|---|---|
| `scout` | Local reads and web research | 12 |
| `browser` | Browser plus local reads; one browser lane at a time | 20 |
| `writer` | Local reads only, with no network | 10 |
| `verifier` | Read-only verification without memory writes | 12 |

A child receives a subset of its parent's permissions and cannot delegate
again.

## Scheduled jobs

```bash
andromeda cron add "every 1h" "summarise recent repository activity"
andromeda cron add "0 9 * * 1-5" "summarise yesterday's commits" --deliver notify
andromeda cron add "in 2h" "check whether the deploy finished"
andromeda cron list
andromeda cron show <id>
andromeda cron logs <id>
andromeda cron run <id>
andromeda cron rm <id>
```

Install the user service, inspect it, or run the daemon in the foreground:

```bash
andromeda cron install
andromeda cron service
andromeda cron daemon
```

### Monitors and scripts

Skip model calls when a source is unchanged:

```bash
andromeda cron add "every 10m" "explain the change" --watch status.sh
andromeda cron add "every 10m" "explain the change" --watch-url https://example.com/status
```

Run a traditional watchdog without a model:

```bash
andromeda cron add "every 10m" --no-agent --script disk-check.sh
```

Scripts are named files under `~/.andromeda-cli/scripts/`, not arbitrary paths.
Supported extensions are `.sh`, `.bash`, and `.py`. Empty stdout means the job
has nothing to deliver.

Jobs can consume a named script with `--script`, chain from another job with
`--after`, and keep a small per-job notepad. Their approval mode is fixed at
creation and cannot be widened by later configuration changes.

## Backup and transfer

```bash
andromeda backup out.tar.gz      # includes credentials; treat it like a password
andromeda export out.tar.gz      # excludes credentials; safe to share
andromeda restore in.tar.gz
```

Restore rejects archive traversal and symlink entries. Use `--force` to replace
existing state.

## Configuration

```bash
andromeda config get
andromeda config get <key>
andromeda config set <key> <value>
andromeda config path
```

Precedence, lowest to highest:

```text
defaults < config file < ANDROMEDA_* environment variables < command flags
```

| Setting | Default |
|---|---|
| `provider` | `relay` |
| `model` | `deepseek/deepseek-v4-flash-0731` |
| `approval_mode` | `ask` |
| `interface` | `repl` |
| `thinking` | `off` |
| `max_tier` | `destructive` |
| `allow_private_network` | `false` |

`ANDROMEDA_HOME` relocates the CLI's configuration, credentials, sessions,
memory, scripts, schedules, and install checkout.

## Paths

| Path | Contents |
|---|---|
| `~/.andromeda-cli/config.yaml` | Non-secret settings |
| `~/.andromeda-cli/credentials.json` | Device token and account metadata |
| `~/.andromeda-cli/mcp.json` | MCP server configuration |
| `~/.andromeda-cli/sessions/` | Saved transcripts and checkpoints |
| `~/.andromeda-cli/memory/` | Local memories |
| `~/.andromeda-cli/scripts/` | Named automation scripts |
| `~/.andromeda-cli/cron/` | Job definitions, state, and run output |

## Maintenance

```bash
andromeda update --check
andromeda update
andromeda doctor
```

Updates are transactional and refuse a dirty install checkout. If dependency
installation fails, the prior revision is restored.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Runtime failure, such as authentication, network, or provider failure |
| `2` | Usage or configuration error |
| `3` | Hosted credit exhausted |
| `130` | Interrupted |
