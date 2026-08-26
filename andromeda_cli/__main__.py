"""Entry point.

`bootstrap.install()` runs before anything that could emit a non-ASCII byte.

Dispatch is hand-rolled rather than argparse subparsers, because the primary
form is a bare prompt — `andromeda "what is 2+2"`. A subparser and a free
positional cannot coexist: argparse binds the first token to the subcommand
choice and rejects anything not in the list, so every bare prompt becomes
"invalid choice". Deciding on the leading token first keeps the bare form the
default and the verbs explicit.
"""

from __future__ import annotations

import os
import sys

from . import bootstrap

bootstrap.install()

import argparse  # noqa: E402

from . import __version__  # noqa: E402
from . import config as config_module  # noqa: E402
from . import completion, output, repl  # noqa: E402
from .commands import (  # noqa: E402
    auth,
    browser_cmd,
    chat,
    config_cmd,
    browser_cmd as _browser_cmd_alias,  # noqa: F401 - kept for clarity below
    approvals,
    cron,
    evals,
    doctor,
    batch_cmd,
    curator_cmd,
    hooks_cmd,
    mcp_cmd,
    secrets_cmd,
    skills_cmd,
    lsp_cmd,
    status as status_cmd,
    worktrees_cmd,
    service,
    transfer,
    memory_cmd,
    profile as profile_cmd,
    sessions as sessions_cmd,
    tools,
    update as update_cmd,
)
from . import sessions as sessions_store  # noqa: E402

COMMANDS = (
    "acp",
    "cloud",
    "batch",
    "pause",
    "resume",
    "setup",
    "auth",
    "cron",
    "completion",
    "curator",
    "hooks",
    "skills",
    "lsp",
    "status",
    "worktrees",
    "eval",
    "mcp",
    "secrets",
    "approvals",
    "backup",
    "export",
    "restore",
    "config",
    "tools",
    "model",
    "sessions",
    "memory",
    "profile",
    "browser",
    "update",
    "doctor",
)

HOOKS_HELP = """Shell scripts run at lifecycle events.

Hooks live in the `hooks:` block of config.yaml, one list per event:

  hooks:
    pre_tool_call:
      - command: ~/.andromeda-cli/hooks/guard.sh
        matcher: terminal          # regex over the tool name, this event only
        timeout: 10                # seconds, 1-300, default 60
        fail_closed: true          # a broken gate blocks instead of allowing
    on_session_end:
      - command: /usr/bin/env python3 ~/hooks/log.py

The script is handed one JSON object on stdin:

  {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
   "tool_input": {...}, "session_id": "...", "cwd": "...", "extra": {...}}

and may print one JSON object on stdout to change what happens:

  {"action": "block",   "message": "not on main"}     stop the call
  {"action": "modify",  "args": {"command": "ls"}}    rewrite the call
  {"action": "approve", "message": "confirm this"}    send it to the gate
  {"context": "..."}                                  pre_llm_call only
  {"output": "..."}                                   transform_* only

Exiting 2 blocks a pre_tool_call whether or not anything was printed.

Each (event, command) pair is approved once, at a prompt, and the approval
records the script's mtime — `andromeda hooks doctor` reports when the file
has changed since. Nothing registers on a run with no terminal unless you
pass --accept-hooks or set hooks_auto_accept.

  andromeda hooks list | doctor
  andromeda hooks test pre_tool_call --for-tool terminal
  andromeda hooks revoke ~/.andromeda-cli/hooks/guard.sh
"""


EPILOG = """
examples:
  andromeda                          start the REPL
  andromeda --tui                    start the full-screen interface
  andromeda "what is 2+2"            one turn, then exit
  git log -5 | andromeda "summarise" read the pipe as part of the prompt

commands:
  andromeda auth login               sign in through your browser
  andromeda auth login <code>        sign in with a code, for a machine with no browser
  andromeda auth status | logout
  andromeda config get [key]
  andromeda hooks list               scripts run at lifecycle events
  andromeda config set <key> <value>
  andromeda config path
  andromeda tools                    list tools and how each is gated
  andromeda tools enable|disable <name>
  andromeda model [id]               show or set the model
  --thinking off|low|medium|high     how hard the model thinks
  andromeda sessions                 recent sessions
  andromeda sessions show <id>
  andromeda sessions search <text>   full-text search across every session
  andromeda sessions recap [id]      what happened, computed not generated
  andromeda sessions export <id> --format html|markdown|jsonl|text
  andromeda sessions active          sessions open in other terminals
  andromeda sessions reindex | doctor | recover
  andromeda sessions rm <id> --force
  --since 7d --until 2026-08-01 --workspace ~/x --model … --role user
  andromeda --resume <id>            pick a session back up
  andromeda --continue               pick the most recent one back up
  andromeda memory                   what it remembers (★ = every prompt)
  andromeda memory search <text>     what it would recall for that
  andromeda memory remember <text> --standing
  andromeda memory forget <text> --force
  andromeda memory export [file.json] | stats
  andromeda profile                  independent installs, one program
  andromeda profile create <name> [--clone|--clone-all]
  andromeda profile use <name> | delete <name> --force
  andromeda -p <name> <anything>     use a profile for one command
  andromeda browser install          add the browser tools
  andromeda browser status
  andromeda cron add "every 1h" "..."   schedule a job
  andromeda cron add "in 2h" "..."      once, later
  andromeda cron add ... --watch s.sh   only run the agent when s.sh's output changes
  andromeda cron add ... --no-agent --script s.sh   a watchdog with no model at all
  andromeda cron list | show <id> | logs <id> | run <id> | rm <id>
  andromeda cron approve <id> --approval auto   let a job change things
  andromeda cron suggest                automations on offer; accept <n> | dismiss <n>
  andromeda cron blueprint              automations as a form; show <key> | use <key> k=v
  andromeda cron executions             every attempt, including interrupted ones
  andromeda cron install                run the scheduler at login
  andromeda cron daemon                 or run it in this terminal
  andromeda eval                     run the behavioural evaluations
  andromeda eval list
  andromeda mcp                      configured MCP servers and their tools
  andromeda mcp example              print a starter mcp.json
  andromeda mcp login <server>       sign in to an MCP server that needs OAuth
  andromeda mcp logout <server>
  andromeda secrets                  vault references, and whether they resolve
  andromeda secrets get <NAME> | schemes | example
  andromeda approvals                tools you have stopped being asked about
  andromeda approvals forget <tool> | clear
  andromeda backup <file.tar.gz>     everything, INCLUDING the device token
  andromeda export <file.tar.gz>     everything except credentials
  andromeda restore <file.tar.gz>
  andromeda doctor                   what is and is not working
  andromeda update [--check]         pull and reinstall, transactionally
"""


def build_parser() -> argparse.ArgumentParser:
    """The chat form: a bare prompt plus per-invocation overrides."""
    parser = argparse.ArgumentParser(
        prog="andromeda",
        description="Andromeda — a local-first agent harness for the terminal.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Run one turn non-interactively and exit. Omit for the REPL.",
    )
    parser.add_argument(
        "--version", action="version", version=f"andromeda {__version__}"
    )
    parser.add_argument("--model", help="Override the model for this invocation.")
    parser.add_argument(
        "--provider",
        choices=["relay", "direct"],
        help="Override the provider lane for this invocation.",
    )
    parser.add_argument(
        "--thinking",
        choices=["off", "low", "medium", "high"],
        help="How hard the model thinks before answering. Costs tokens and time.",
    )
    parser.add_argument(
        "--approval",
        choices=["auto", "ask", "deny"],
        help="Override the approval mode. `auto` does not ask before changing files.",
    )
    # Two flags rather than one with a value, because both are used to *override*
    # the `interface` setting and a bare `--tui` reads better than `--ui tui`.
    surface = parser.add_mutually_exclusive_group()
    surface.add_argument(
        "--tui",
        dest="interface",
        action="store_const",
        const="tui",
        help="Full-screen interface. Needs a terminal on stdin and stdout.",
    )
    surface.add_argument(
        "--no-tui",
        dest="interface",
        action="store_const",
        const="repl",
        help="The line-based REPL, even if `interface: tui` is configured.",
    )
    parser.add_argument(
        "--workspace",
        help="Directory the agent may reach. Defaults to the current directory.",
    )
    parser.add_argument(
        "--accept-hooks",
        action="store_true",
        help="Approve the shell hooks in your config without a prompt, this run.",
    )
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument("--resume", metavar="ID", help="Resume a saved session by id.")
    resume.add_argument(
        "--continue",
        dest="continue_last",
        action="store_true",
        help="Resume the most recent session.",
    )
    return parser


def build_command_parser() -> argparse.ArgumentParser:
    """The verb forms. Reached only when argv leads with a known command."""
    parser = argparse.ArgumentParser(prog="andromeda")
    sub = parser.add_subparsers(dest="command", required=True)

    auth_parser = sub.add_parser("auth", help="Sign this machine in or out.")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser(
        "login", help="Sign in through your browser, or with a code."
    )
    login.add_argument(
        "code",
        nargs="?",
        help="A pairing code from your account page. Omit it to sign in through your browser.",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the sign-in URL instead of opening it. For ssh and remote shells.",
    )
    auth_sub.add_parser("status", help="Show whether this machine is signed in.")
    auth_sub.add_parser("logout", help="Delete the device token from this machine.")

    config_parser = sub.add_parser("config", help="Read or write settings.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    get = config_sub.add_parser("get", help="Show one setting, or all of them.")
    get.add_argument("key", nargs="?")
    set_cmd = config_sub.add_parser("set", help="Write one setting.")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value")
    config_sub.add_parser("path", help="Print the Andromeda home directory.")

    tools_parser = sub.add_parser("tools", help="List or switch tools.")
    tools_sub = tools_parser.add_subparsers(dest="tools_command")
    enable = tools_sub.add_parser("enable", help="Turn one tool on.")
    enable.add_argument("name")
    disable = tools_sub.add_parser("disable", help="Turn one tool off.")
    disable.add_argument("name")

    model_parser = sub.add_parser("model", help="Show or set the model.")
    model_parser.add_argument("id", nargs="?", help="Model id to switch to.")

    update_parser = sub.add_parser("update", help="Update this install.")
    update_parser.add_argument(
        "--check", action="store_true", help="Report what is available, change nothing."
    )
    doctor_parser = sub.add_parser("doctor", help="Show what is and is not working.")
    doctor_parser.add_argument(
        "--cloud",
        action="store_true",
        help=(
            "Also check what only a hosted runner can answer: the binaries a "
            "job shells out to, that ANDROMEDA_HOME is the mounted volume and "
            "not the image layer, free space, and that no model key is present."
        ),
    )
    sub.add_parser("setup", help="First-run setup. Four questions, all skippable.")

    backup_parser = sub.add_parser(
        "backup", help="Archive everything, including credentials."
    )
    backup_parser.add_argument("path")
    export_parser = sub.add_parser(
        "export", help="Archive everything except credentials."
    )
    export_parser.add_argument("path")
    restore_parser = sub.add_parser("restore", help="Restore an archive into this install.")
    restore_parser.add_argument("path")
    restore_parser.add_argument(
        "--force", action="store_true", help="Overwrite existing state."
    )

    # No subparser here: `eval` takes an optional name pattern, and argparse
    # cannot hold both a subcommand list and a free positional — the pattern
    # would be read as an invalid subcommand. `list` is dispatched by hand
    # below, the same way the top level dispatches its verbs.
    eval_parser = sub.add_parser("eval", help="Behavioural evaluations.")
    eval_parser.add_argument(
        "pattern",
        nargs="?",
        default="",
        help=(
            "Only scenarios whose name contains this. `list` to list them, "
            "`report` for what moved since the last run, `runs` for the history."
        ),
    )
    eval_parser.add_argument("--json", action="store_true", dest="as_json")
    eval_parser.add_argument("--root", help="Directory of scenarios.")
    eval_parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Run each scenario this many times and report a pass rate. An "
            "agent is stochastic; one run is an anecdote."
        ),
    )
    eval_parser.add_argument(
        "--jobs", type=int, default=1, help="How many scenarios to run at once."
    )

    cron_parser = sub.add_parser("cron", help="Scheduled jobs.")
    cron_sub = cron_parser.add_subparsers(dest="cron_command")

    cron_add = cron_sub.add_parser("add", help="Schedule a job.")
    cron_add.add_argument(
        "schedule", help="'every 30m', a cron expression, 'in 2h', or 'at 09:00'."
    )
    cron_add.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="What the job should do. Omit only with --no-agent.",
    )
    cron_add.add_argument("--name", default="", help="Short name for the job.")
    cron_add.add_argument(
        "--approval",
        choices=["ask", "auto", "deny"],
        default="ask",
        help=(
            "What this job may do, decided now because nobody is watching when "
            "it runs. `ask` gets read-only tools; `auto` may change things."
        ),
    )
    cron_add.add_argument("--workspace", help="Directory to run in. Defaults to here.")
    cron_add.add_argument(
        "--cloud",
        action="store_true",
        help=(
            "Run on a hosted runner instead of this machine, so it fires "
            "whether or not this computer is awake. Needs --detached: a "
            "container cannot see your files."
        ),
    )
    cron_add.add_argument(
        "--repo",
        dest="repo_url",
        default="",
        metavar="URL",
        help=(
            "Work on a fresh clone of this https remote each run, and push what "
            "changed onto a branch this run creates. Never onto your default one."
        ),
    )
    cron_add.add_argument(
        "--repo-ref", dest="repo_ref", default="", help="Branch to clone from."
    )
    cron_add.add_argument(
        "--detached",
        action="store_true",
        help=(
            "Give this job no filesystem at all — the network, its notepad and "
            "its memory, and nothing else. Required with --cloud."
        ),
    )
    cron_add.add_argument(
        "--repeat",
        type=int,
        default=0,
        help="Run this many times, then retire. Omit for forever.",
    )
    cron_add.add_argument(
        "--deliver",
        choices=["none", "notify", "stdout"],
        default="none",
        help=(
            "How you hear about a run. The output is always saved either way — "
            "`andromeda cron logs` reads it."
        ),
    )
    cron_add.add_argument(
        "--script",
        default="",
        help=(
            "A script in ~/.andromeda-cli/scripts/ whose output goes into the "
            "prompt as fresh facts."
        ),
    )
    cron_add.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_true",
        help=(
            "The script IS the job — its output is the report and no model runs. "
            "No output means nothing is reported."
        ),
    )
    cron_add.add_argument(
        "--watch",
        default="",
        help=(
            "A script to run first each tick. The agent runs only when its "
            "output changes, so an unchanged tick costs nothing."
        ),
    )
    cron_add.add_argument(
        "--watch-url",
        dest="watch_url",
        default="",
        help="Same as --watch, fetching a URL instead of running a script.",
    )
    cron_add.add_argument(
        "--after",
        action="append",
        default=[],
        metavar="JOB",
        help="Put this job's latest output into the prompt. Repeatable.",
    )
    cron_add.add_argument(
        "--deliver-to",
        dest="deliver_target",
        default="",
        metavar="URL",
        help="Where `--deliver webhook` posts.",
    )
    cron_add.add_argument("--model", default="", help="Override the model for this job.")
    cron_add.add_argument(
        "--thinking",
        default="",
        choices=["", "off", "low", "medium", "high"],
        help="Override how hard the model thinks, for this job.",
    )
    cron_add.add_argument(
        "--tools",
        default="",
        metavar="A,B",
        help=(
            "Narrow this job's toolbelt to these names. Fewer tools is fewer "
            "schemas in every prompt it ever sends."
        ),
    )
    cron_add.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="NAME",
        help="A skill this job should load before it starts. Repeatable.",
    )
    cron_add.add_argument(
        "--attach",
        default="",
        metavar="SESSION",
        help="Append each run to this session, so it shows up in --resume.",
    )

    cron_sub.add_parser("list", help="List scheduled jobs.")
    cron_show = cron_sub.add_parser("show", help="One job and its recent runs.")
    cron_show.add_argument("id")
    cron_logs = cron_sub.add_parser("logs", help="The full output of a run.")
    cron_logs.add_argument("id")
    cron_logs.add_argument(
        "-n", type=int, default=0, dest="index", help="How many runs back. 0 is latest."
    )
    cron_run = cron_sub.add_parser("run", help="Run a job now.")
    cron_run.add_argument("id")
    cron_rm = cron_sub.add_parser("rm", help="Delete a job.")
    cron_rm.add_argument("id")
    cron_enable = cron_sub.add_parser("enable", help="Enable a job.")
    cron_enable.add_argument("id")
    cron_disable = cron_sub.add_parser("disable", help="Disable a job.")
    cron_disable.add_argument("id")
    cron_resume = cron_sub.add_parser(
        "resume", help="Clear a job's self-imposed pause and put it back on cadence."
    )
    cron_resume.add_argument("id")
    cron_approve = cron_sub.add_parser(
        "approve", help="Change what an existing job may do."
    )
    cron_approve.add_argument("id")
    cron_approve.add_argument(
        "--approval", choices=["ask", "auto", "deny"], default=""
    )
    cron_approve.add_argument(
        "--run-on",
        dest="run_on",
        choices=["device", "cloud"],
        default="",
        help=(
            "Move this job to a hosted runner, or back. A separate grant from "
            "--approval: it decides whose hardware holds your credentials."
        ),
    )
    cron_approve.add_argument(
        "--detached",
        action="store_true",
        help="Also drop this job's filesystem access. Usually needed with --run-on cloud.",
    )
    cron_notepad = cron_sub.add_parser("notepad", help="What a job remembers.")
    cron_notepad.add_argument("id")
    cron_notepad.add_argument(
        "action", nargs="?", default="list", choices=["list", "set", "forget", "clear"]
    )
    cron_notepad.add_argument("key", nargs="?", default="")
    cron_notepad.add_argument("value", nargs="?", default="")
    cron_daemon = cron_sub.add_parser("daemon", help="Run due jobs until stopped.")
    cron_daemon.add_argument(
        "--once", action="store_true", help="Run whatever is due, then exit."
    )
    cron_suggest = cron_sub.add_parser("suggest", help="Automations on offer.")
    cron_suggest.add_argument(
        "suggest_command", nargs="?", default="list", choices=["list", "accept", "dismiss"]
    )
    cron_suggest.add_argument("ref", nargs="?", default="")
    cron_suggest.add_argument("--workspace", help="Directory the job runs in.")

    cron_blueprint = cron_sub.add_parser("blueprint", help="Automations as a form.")
    cron_blueprint.add_argument(
        "blueprint_command", nargs="?", default="list", choices=["list", "show", "use"]
    )
    cron_blueprint.add_argument("key", nargs="?", default="")
    cron_blueprint.add_argument(
        "values", nargs="*", default=[], metavar="name=value", help="Slot values, for `use`."
    )
    cron_blueprint.add_argument("--workspace", help="Directory the job runs in.")

    cron_exec = cron_sub.add_parser(
        "executions", help="Every attempt, including ones nobody recorded the end of."
    )
    cron_exec.add_argument("id", nargs="?", default="")
    cron_exec.add_argument(
        "--unresolved",
        action="store_true",
        help="Only attempts that never reached a terminal state.",
    )

    cron_serve = cron_sub.add_parser(
        "serve",
        help="Answer fires from a hosted scheduler. The runner's whole job.",
    )
    cron_serve.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address. The default is right inside a container and wrong on a laptop.",
    )
    cron_serve.add_argument("--port", type=int, default=8080)

    cron_push = cron_sub.add_parser(
        "push", help="Arm a cloud job on the server, or every cloud job."
    )
    cron_push.add_argument("id", nargs="?", default="")

    cron_runs = cron_sub.add_parser(
        "runs", help="What the hosted runner has done while you were away."
    )
    cron_runs.add_argument("-n", type=int, default=20, dest="limit")

    cron_fires = cron_sub.add_parser(
        "fires", help="Every fire this machine was asked to run, and what became of it."
    )
    cron_fires.add_argument("id", nargs="?", default="")
    cron_fires.add_argument(
        "--unresolved",
        action="store_true",
        help="Only fires whose lease ran out with nothing recorded.",
    )

    cron_sub.add_parser("install", help="Run the scheduler in the background, at login.")
    cron_sub.add_parser("uninstall", help="Remove the background scheduler.")
    cron_sub.add_parser("service", help="Whether the background scheduler is installed.")

    hooks_parser = sub.add_parser(
        "hooks",
        help="Shell scripts run at lifecycle events.",
        description=HOOKS_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hooks_sub = hooks_parser.add_subparsers(dest="hooks_command")
    hooks_sub.add_parser("list", help="Configured hooks, and whether they may run.")
    hooks_test = hooks_sub.add_parser("test", help="Fire one event now.")
    hooks_test.add_argument("event", help="The event to fire.")
    hooks_test.add_argument(
        "--for-tool", default="", help="Pretend the call was this tool."
    )
    hooks_test.add_argument(
        "--payload-file", default="", help="JSON object merged into the payload."
    )
    hooks_revoke = hooks_sub.add_parser("revoke", help="Withdraw an approval.")
    # `target`, not `command`: the top-level subparser already writes its verb
    # to `args.command`, and a second argument of that name silently overwrites
    # it — `hooks revoke X` then dispatches as though the verb were X.
    hooks_revoke.add_argument(
        "target", help="The command line, exactly as it appears in the config."
    )
    hooks_sub.add_parser("doctor", help="Check every configured hook.")

    sub.add_parser(
        "acp",
        help="Speak the Agent Client Protocol on stdin/stdout, for an editor.",
        description=(
            "Runs this agent as an editor's, over the Agent Client Protocol.\n"
            "Point your editor's ACP agent configuration at:\n\n"
            "    andromeda acp\n\n"
            "Nothing else may write to stdout while it runs — the protocol is "
            "the stream."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    curator_parser = sub.add_parser(
        "curator", help="The skill library, and keeping it honest."
    )
    curator_sub = curator_parser.add_subparsers(dest="curator_command")
    curator_status = curator_sub.add_parser("status", help="What is tracked, and its state.")
    curator_status.add_argument("--workspace", default="")
    curator_sweep = curator_sub.add_parser(
        "sweep", help="Move skills between active, stale and archived."
    )
    curator_sweep.add_argument("--workspace", default="")
    curator_sweep.add_argument(
        "--dry-run", action="store_true", help="Say what would move, change nothing."
    )
    curator_review = curator_sub.add_parser(
        "review", help="Ask a model what it would change. Proposals only."
    )
    curator_review.add_argument("--workspace", default="")
    curator_review.add_argument(
        "--show", action="store_true", help="Print the last proposals without a new run."
    )
    curator_pin = curator_sub.add_parser("pin", help="Never sweep this one.")
    curator_pin.add_argument("name")
    curator_pin.add_argument("--workspace", default="")
    curator_unpin = curator_sub.add_parser("unpin", help="Take that back.")
    curator_unpin.add_argument("name")
    curator_unpin.add_argument("--workspace", default="")
    curator_restore = curator_sub.add_parser("restore", help="Bring an archived skill back.")
    curator_restore.add_argument("name")
    curator_sub.add_parser("pause", help="Stop sweeping until further notice.")
    curator_sub.add_parser("resume", help="Start again.")

    completion_parser = sub.add_parser(
        "completion",
        help="Print a shell completion script.",
        description=(
            "Generated from this program's own argument parser, so it is never "
            "out of date.\n\n"
            "  bash:  eval \"$(andromeda completion bash)\"   in ~/.bashrc\n"
            "  zsh:   eval \"$(andromeda completion zsh)\"    in ~/.zshrc\n"
            "  fish:  andromeda completion fish | source     in your config"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    completion_parser.add_argument("shell", choices=list(completion.SHELLS))

    skills_parser = sub.add_parser(
        "skills", help="Skills on this machine, and what the scan found."
    )
    skills_sub = skills_parser.add_subparsers(dest="skills_command")
    skills_list = skills_sub.add_parser("list", help="Every skill, and its verdict.")
    skills_list.add_argument("--workspace", default="", help="Directory to look in.")
    skills_scan = skills_sub.add_parser(
        "scan", help="What a skill contains that is worth knowing about."
    )
    skills_scan.add_argument("name", nargs="?", default="", help="One skill, or all.")
    skills_scan.add_argument("--workspace", default="", help="Directory to look in.")
    skills_trust = skills_sub.add_parser(
        "trust", help="Use a withheld skill anyway, at its current content."
    )
    skills_trust.add_argument("name")
    skills_trust.add_argument("--workspace", default="", help="Directory to look in.")
    skills_untrust = skills_sub.add_parser("untrust", help="Take that back.")
    skills_untrust.add_argument("name")

    status_parser = sub.add_parser(
        "status", help="What this install is set to, and what it has spent."
    )
    status_parser.add_argument(
        "--days", type=int, default=7, help="How far back the usage total reaches."
    )

    lsp_parser = sub.add_parser(
        "lsp", help="Language servers used for diagnostics after an edit."
    )
    lsp_sub = lsp_parser.add_subparsers(dest="lsp_command")
    lsp_status = lsp_sub.add_parser(
        "status", help="What would run here, and what is missing."
    )
    lsp_status.add_argument("--path", default="", help="Directory to inspect.")
    lsp_sub.add_parser("servers", help="Every language server this harness knows.")

    worktrees_parser = sub.add_parser(
        "worktrees", help="Working copies delegated lanes left behind."
    )
    worktrees_sub = worktrees_parser.add_subparsers(dest="worktrees_command")
    worktrees_list = worktrees_sub.add_parser("list", help="Every lane worktree.")
    worktrees_list.add_argument("--repo", default="", help="Repository to inspect.")
    worktrees_prune = worktrees_sub.add_parser(
        "prune", help="Remove the ones holding nothing."
    )
    worktrees_prune.add_argument("--repo", default="", help="Repository to inspect.")
    worktrees_prune.add_argument(
        "--dry-run", action="store_true", help="Say what would go, change nothing."
    )

    batch_parser = sub.add_parser(
        "batch",
        help="Run one prompt over every row of a file.",
        description=(
            "Each row gets its own conversation, and its answer is appended to "
            "a results file the moment it lands — so a run that stops half-way "
            "is resumed rather than repeated.\n\n"
            "  andromeda batch tickets.jsonl --prompt 'Classify: {body}'\n"
            "  andromeda batch tickets.jsonl --resume\n\n"
            "A JSONL row's `id` is what --resume matches on. Rows without one "
            "are matched by line number, so reordering the file breaks a resume."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    batch_parser.add_argument(
        "path",
        nargs="?",
        default="",
        help="A .jsonl file, or one item per line. Omit with --show.",
    )
    batch_parser.add_argument(
        "--prompt",
        default="",
        help="Template. {field} is replaced from each row; omit to use its `prompt`.",
    )
    batch_parser.add_argument("--out", default="", help="Where results go.")
    batch_parser.add_argument(
        "--jobs", type=int, default=1, help="How many rows to run at once."
    )
    batch_parser.add_argument(
        "--resume", action="store_true", help="Skip rows already in the results."
    )
    batch_parser.add_argument("--workspace", default="", help="Directory to run in.")
    batch_parser.add_argument(
        "--dry-run", action="store_true", help="Say what would run, spend nothing."
    )
    batch_parser.add_argument(
        "--show", default="", metavar="RESULTS", help="Print a results file instead."
    )
    batch_parser.add_argument(
        "--failures", action="store_true", help="With --show, only the failures."
    )

    pause_parser = sub.add_parser(
        "pause",
        help="Hold scheduled jobs. Your own terminal is unaffected.",
        description=(
            "Stops the scheduler firing anything new. Work already running is "
            "never killed, and an interactive session is never touched — this "
            "holds the work nobody is watching. `andromeda resume` lifts it, "
            "and the next tick picks up with no restart."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pause_parser.add_argument(
        "--reason", default="", help="Why, for whoever reads it later."
    )
    sub.add_parser("resume", help="Lift the hold set by `andromeda pause`.")

    approvals_parser = sub.add_parser("approvals", help="Learned approvals.")
    approvals_sub = approvals_parser.add_subparsers(dest="approvals_command")
    forget = approvals_sub.add_parser("forget", help="Ask about this tool again.")
    forget.add_argument("tool")
    approvals_sub.add_parser("clear", help="Forget every learned approval.")
    approvals_test = approvals_sub.add_parser(
        "test", help="What the gate would do with a tool, without running it."
    )
    approvals_test.add_argument("tool")
    approvals_test.add_argument(
        "--mode",
        default="",
        choices=["auto", "ask", "deny"],
        help="Try a different approval mode than the configured one.",
    )
    approvals_test.add_argument("--workspace", default="")
    approvals_suggest = approvals_sub.add_parser(
        "suggest", help="Tools you have approved often enough to stop being asked."
    )
    approvals_suggest.add_argument(
        "--apply", default="", help="Numbers from the list, comma separated."
    )

    cloud_parser = sub.add_parser(
        "cloud", help="Your hosted runner — the thing that fires jobs while you are away."
    )
    cloud_sub = cloud_parser.add_subparsers(dest="cloud_command")
    cloud_up = cloud_sub.add_parser("up", help="Register a runner you have deployed.")
    cloud_up.add_argument(
        "endpoint",
        nargs="?",
        default="",
        help="Its https URL — `modal deploy cli/modal_app.py` prints one.",
    )
    cloud_up.add_argument("--provider", default="modal")
    cloud_sub.add_parser("status", help="Whether a runner exists and is answering.")
    cloud_down = cloud_sub.add_parser(
        "down", help="Disarm every cloud job and revoke the runner's credential."
    )
    cloud_down.add_argument(
        "--yes", action="store_true", help="Required. It revokes a credential."
    )

    secrets_parser = sub.add_parser(
        "secrets", help="Credentials resolved from a vault."
    )
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_command")
    secrets_get = secrets_sub.add_parser("get", help="Check one, masked.")
    secrets_get.add_argument("name", help="The environment-variable name.")
    secrets_put = secrets_sub.add_parser(
        "put", help="Store a credential a hosted job can use."
    )
    secrets_put.add_argument("name", help="The environment variable it becomes.")
    secrets_put.add_argument(
        "value",
        nargs="?",
        default="",
        help="Omit it and you are prompted — an argument is a line in your shell history.",
    )
    secrets_put.add_argument(
        "--cloud",
        action="store_true",
        help=(
            "Required. Every other scheme references THIS machine and cannot "
            "follow a job into a container; this is the one that can."
        ),
    )
    secrets_list = secrets_sub.add_parser(
        "list", help="Hosted secrets, by name. Never values."
    )
    secrets_list.add_argument("--cloud", action="store_true")
    secrets_forget = secrets_sub.add_parser("forget", help="Remove a hosted secret.")
    secrets_forget.add_argument("name")
    secrets_forget.add_argument("--cloud", action="store_true")
    secrets_sub.add_parser("schemes", help="What this build can read from.")
    secrets_sub.add_parser("example", help="Print a starter `secrets:` block.")

    mcp_parser = sub.add_parser("mcp", help="Configured MCP servers.")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_sub.add_parser("example", help="Print a starter mcp.json.")
    mcp_login = mcp_sub.add_parser("login", help="Authorize an OAuth MCP server.")
    mcp_login.add_argument("server", help="The name from mcp.json.")
    mcp_logout = mcp_sub.add_parser("logout", help="Forget an MCP server's tokens.")
    mcp_logout.add_argument("server", help="The name from mcp.json.")

    browser_parser = sub.add_parser("browser", help="The browser tools.")
    browser_sub = browser_parser.add_subparsers(dest="browser_command")
    browser_sub.add_parser("install", help="Install Playwright and Chromium.")
    browser_sub.add_parser("status", help="Show whether the browser tools are usable.")

    sessions_parser = sub.add_parser("sessions", help="Past sessions.")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_command")

    def _add_filters(target: argparse.ArgumentParser) -> None:
        """The same narrowing on list and on search.

        Defined once rather than declared twice: two copies of a filter set is
        how `--since` ends up meaning one thing in a listing and another in a
        search.
        """
        target.add_argument("--since", default="", help="7d, yesterday, 2026-08-01.")
        target.add_argument("--until", default="", help="The other end of the range.")
        target.add_argument("--workspace", default="", help="Match the workspace path.")
        target.add_argument("--model", default="", help="Match the model.")
        target.add_argument(
            "--provider", default="", help="Match the provider lane exactly."
        )
        target.add_argument(
            "--limit", type=int, default=sessions_store.LIST_LIMIT, help="How many."
        )

    listing = sessions_sub.add_parser("list", help="Recent sessions.")
    _add_filters(listing)

    show = sessions_sub.add_parser("show", help="Print one session's transcript.")
    show.add_argument("id")
    show.add_argument(
        "--live-only",
        dest="live_only",
        action="store_true",
        help="Only what is still in the conversation, not the turns it compacted out.",
    )

    find = sessions_sub.add_parser("search", help="Find sessions containing text.")
    find.add_argument("query")
    find.add_argument(
        "--role", default="", help="Only these roles: user, assistant, tool."
    )
    _add_filters(find)

    recap_cmd = sessions_sub.add_parser(
        "recap", help="What happened in a session, without asking the model."
    )
    recap_cmd.add_argument("id", nargs="?", default="")

    export_cmd = sessions_sub.add_parser("export", help="Write a session out.")
    export_cmd.add_argument("id")
    export_cmd.add_argument(
        "--format",
        dest="fmt",
        default="markdown",
        choices=["markdown", "md", "html", "jsonl", "text", "txt"],
    )
    export_cmd.add_argument(
        "-o", "--out", dest="out", default="", help="File or directory. Default: stdout."
    )

    remove_cmd = sessions_sub.add_parser("rm", help="Delete a session for good.")
    remove_cmd.add_argument("id")
    remove_cmd.add_argument("--force", action="store_true")

    reindex_cmd = sessions_sub.add_parser("reindex", help="Catch the index up.")
    reindex_cmd.add_argument(
        "--force", action="store_true", help="Rebuild every session, not just stale ones."
    )

    sessions_sub.add_parser("doctor", help="Whether everything is readable and indexed.")

    recover_cmd = sessions_sub.add_parser("recover", help="Salvage damaged transcripts.")
    recover_cmd.add_argument(
        "--apply", action="store_true", help="Actually write the salvage back."
    )
    recover_cmd.add_argument(
        "--rebuild-index",
        dest="rebuild_index",
        action="store_true",
        help="Throw the index away and build it again from the transcripts.",
    )

    sessions_sub.add_parser("active", help="Sessions open in other terminals now.")

    memory_parser = sub.add_parser("memory", help="What the agent remembers.")
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    memory_list = memory_sub.add_parser("list", help="Everything it remembers.")
    memory_list.add_argument(
        "--scope", default="", choices=["standing", "episode"], help="Only this tier."
    )
    memory_find = memory_sub.add_parser(
        "search", help="What it would recall for a query."
    )
    memory_find.add_argument("query")
    memory_find.add_argument("--limit", type=int, default=10)
    memory_add = memory_sub.add_parser(
        "remember", help="Teach it something without spending a turn."
    )
    memory_add.add_argument("content")
    memory_add.add_argument(
        "--standing",
        dest="scope",
        action="store_const",
        const="standing",
        default="episode",
        help="Load it into every prompt. Keep these few.",
    )
    memory_add.add_argument("--tags", default="", help="Comma separated.")
    memory_drop = memory_sub.add_parser("forget", help="Take something back.")
    memory_drop.add_argument("query")
    memory_drop.add_argument(
        "--scope", default="any", choices=["standing", "episode", "any"]
    )
    memory_drop.add_argument("--force", action="store_true")
    memory_export = memory_sub.add_parser(
        "export", help="Every memory as JSON, on any backend."
    )
    memory_export.add_argument("path", nargs="?", default="")
    memory_sub.add_parser("stats", help="How much is stored, and where.")

    profile_parser = sub.add_parser("profile", help="Independent installs.")
    profile_sub = profile_parser.add_subparsers(dest="profile_command")
    profile_create = profile_sub.add_parser("create", help="Make a new profile.")
    profile_create.add_argument("name")
    profile_create.add_argument(
        "--clone", action="store_true", help="Copy settings, SOUL.md and skills."
    )
    profile_create.add_argument(
        "--clone-all",
        dest="clone_all",
        action="store_true",
        help="Copy everything except credentials and runtime state.",
    )
    profile_use = profile_sub.add_parser("use", help="Make a profile the default.")
    profile_use.add_argument("name")
    profile_delete = profile_sub.add_parser("delete", help="Remove a profile entirely.")
    profile_delete.add_argument("name")
    profile_delete.add_argument("--force", action="store_true")
    profile_sub.add_parser("list", help="Every profile on this machine.")
    profile_sub.add_parser("current", help="Which profile this command is using.")

    return parser


def _config(args: argparse.Namespace) -> dict:
    values = config_module.load()
    # Flags beat the file and the environment: they are the most local
    # statement of intent in play.
    if getattr(args, "model", None):
        values["model"] = args.model
    if getattr(args, "provider", None):
        values["provider"] = args.provider
    if getattr(args, "approval", None):
        values["approval_mode"] = args.approval
    if getattr(args, "thinking", None):
        values["thinking"] = args.thinking
    if getattr(args, "interface", None):
        values["interface"] = args.interface
    return values


def _run_command(argv: list[str]) -> int:
    args = build_command_parser().parse_args(argv)

    if args.command == "auth":
        if args.auth_command == "login":
            return auth.login(
                args.code,
                base_url=str(config_module.load()["base_url"]),
                open_browser=not args.no_browser,
            )
        if args.auth_command == "status":
            return auth.status()
        return auth.logout()

    if args.command == "tools":
        if args.tools_command == "enable":
            return tools.enable(args.name)
        if args.tools_command == "disable":
            return tools.disable(args.name)
        return tools.show()

    if args.command == "update":
        return update_cmd.run(check_only=args.check)

    if args.command == "hooks":
        if args.hooks_command == "test":
            return hooks_cmd.test(
                args.event, for_tool=args.for_tool, payload_file=args.payload_file
            )
        if args.hooks_command == "revoke":
            return hooks_cmd.revoke(args.target)
        if args.hooks_command == "doctor":
            return hooks_cmd.doctor()
        return hooks_cmd.show_list()

    if args.command == "batch":
        if args.show:
            return batch_cmd.show(args.show, failures_only=args.failures)
        if not args.path:
            output.fail("andromeda batch needs a file.", "andromeda batch rows.jsonl")
            return 2
        return batch_cmd.run(
            args.path,
            prompt=args.prompt,
            out=args.out,
            jobs=args.jobs,
            resume=args.resume,
            workspace=args.workspace,
            dry_run=args.dry_run,
        )

    if args.command in {"pause", "resume"}:
        from andromeda_agent import pause as pause_module

        home = config_module.home()
        if args.command == "resume":
            if pause_module.disengage(home):
                output.ok("Resumed. Scheduled jobs pick up on the next tick.")
            else:
                output.info("Not paused.")
            return 0
        path = pause_module.engage(home, args.reason)
        detail = f" — {args.reason}" if args.reason else ""
        output.ok(f"Paused{detail}.")
        output.info(f"  {path}")
        output.info(
            "  Scheduled jobs are on hold. Work already running is untouched, "
            "and your own sessions are unaffected."
        )
        return 0

    if args.command == "acp":
        from .commands import acp_cmd

        return acp_cmd.run()

    if args.command == "curator":
        if args.curator_command == "sweep":
            return curator_cmd.sweep(args.workspace, dry_run=args.dry_run)
        if args.curator_command == "review":
            return curator_cmd.review(args.workspace, show_only=args.show)
        if args.curator_command == "pin":
            return curator_cmd.pin(args.name, args.workspace)
        if args.curator_command == "unpin":
            return curator_cmd.unpin(args.name, args.workspace)
        if args.curator_command == "restore":
            return curator_cmd.restore(args.name)
        if args.curator_command == "pause":
            return curator_cmd.pause()
        if args.curator_command == "resume":
            return curator_cmd.resume()
        return curator_cmd.status(getattr(args, "workspace", ""))

    if args.command == "completion":
        # The verb parser, not the bare one: completion is for the verbs, and
        # the bare form is a free-text prompt with nothing to complete.
        print(completion.generate(args.shell, build_command_parser()), end="")
        return 0

    if args.command == "skills":
        if args.skills_command == "scan":
            return skills_cmd.scan(args.name, workspace=args.workspace)
        if args.skills_command == "trust":
            return skills_cmd.trust(args.name, workspace=args.workspace)
        if args.skills_command == "untrust":
            return skills_cmd.untrust(args.name)
        return skills_cmd.show_list(getattr(args, "workspace", ""))

    if args.command == "status":
        return status_cmd.run(days=max(1, int(getattr(args, "days", 7))))

    if args.command == "lsp":
        if args.lsp_command == "servers":
            return lsp_cmd.servers()
        return lsp_cmd.status(getattr(args, "path", ""))

    if args.command == "worktrees":
        if args.worktrees_command == "prune":
            return worktrees_cmd.prune(args.repo, dry_run=args.dry_run)
        return worktrees_cmd.show_list(getattr(args, "repo", ""))

    if args.command == "cloud":
        from .commands import cloud_cmd

        if args.cloud_command == "up":
            return cloud_cmd.up(args.endpoint, provider=args.provider)
        if args.cloud_command == "status":
            return cloud_cmd.status()
        if args.cloud_command == "down":
            return cloud_cmd.down(yes=args.yes)
        cloud_parser.print_help()
        return 2

    if args.command == "doctor":
        return doctor.run(cloud=args.cloud)

    if args.command == "setup":
        from .commands import setup as setup_cmd

        return setup_cmd.run()

    if args.command == "backup":
        return transfer.backup(args.path)
    if args.command == "export":
        return transfer.export(args.path)
    if args.command == "restore":
        return transfer.restore(args.path, force=args.force)

    if args.command == "eval":
        if args.pattern == "list":
            return evals.show_list(root=args.root)
        if args.pattern == "report":
            return evals.report(root=args.root)
        if args.pattern == "runs":
            return evals.show_runs()
        return evals.run(
            pattern=args.pattern,
            as_json=args.as_json,
            root=args.root,
            repeat=args.repeat,
            jobs=args.jobs,
        )

    if args.command == "cron":
        if args.cron_command == "add":
            return cron.add(
                args.schedule,
                args.prompt,
                name=args.name,
                approval=args.approval,
                workspace=args.workspace,
                repeat=args.repeat,
                deliver=args.deliver,
                script=args.script,
                no_agent=args.no_agent,
                watch=args.watch,
                watch_url=args.watch_url,
                after=args.after,
                deliver_target=args.deliver_target,
                model=args.model,
                thinking=args.thinking,
                tools=args.tools,
                skills=args.skill,
                attach_to=args.attach,
                cloud=args.cloud,
                detached=args.detached,
                repo_url=args.repo_url,
                repo_ref=args.repo_ref,
            )
        if args.cron_command == "show":
            return cron.show(args.id)
        if args.cron_command == "logs":
            return cron.logs(args.id, index=args.index)
        if args.cron_command == "run":
            return cron.run_now(args.id)
        if args.cron_command == "rm":
            return cron.remove(args.id)
        if args.cron_command == "enable":
            return cron.enable(args.id, True)
        if args.cron_command == "disable":
            return cron.enable(args.id, False)
        if args.cron_command == "resume":
            return cron.resume(args.id)
        if args.cron_command == "approve":
            return cron.approve(
                args.id,
                args.approval,
                run_on=args.run_on,
                detached=args.detached,
            )
        if args.cron_command == "notepad":
            return cron.notepad(args.id, args.action, args.key, args.value)
        if args.cron_command == "daemon":
            return cron.daemon(once=args.once)
        if args.cron_command == "serve":
            return cron.serve(host=args.host, port=args.port)
        if args.cron_command == "push":
            return cron.push(args.id)
        if args.cron_command == "runs":
            return cron.runs(limit=args.limit)
        if args.cron_command == "fires":
            return cron.fires(args.id, unresolved_only=args.unresolved)
        if args.cron_command == "suggest":
            if args.suggest_command == "accept":
                return cron.suggest_accept(args.ref, workspace=args.workspace)
            if args.suggest_command == "dismiss":
                return cron.suggest_dismiss(args.ref)
            return cron.suggest_list()
        if args.cron_command == "blueprint":
            if args.blueprint_command == "show":
                return cron.blueprint_show(args.key)
            if args.blueprint_command == "use":
                return cron.blueprint_use(args.key, args.values, workspace=args.workspace)
            return cron.blueprint_list()
        if args.cron_command == "executions":
            return cron.executions(args.id, unresolved_only=args.unresolved)
        if args.cron_command == "install":
            return service.install()
        if args.cron_command == "uninstall":
            return service.uninstall()
        if args.cron_command == "service":
            return service.status()
        return cron.show_list()

    if args.command == "approvals":
        if args.approvals_command == "forget":
            return approvals.forget(args.tool)
        if args.approvals_command == "clear":
            return approvals.clear()
        if args.approvals_command == "test":
            return approvals.test(args.tool, mode=args.mode, workspace=args.workspace)
        if args.approvals_command == "suggest":
            return approvals.suggest(apply=args.apply)
        return approvals.show()

    if args.command == "secrets":
        if args.secrets_command == "get":
            return secrets_cmd.get(args.name)
        if args.secrets_command == "put":
            if not args.cloud:
                # Refused rather than defaulted. A local `secrets:` block is a
                # reference somebody writes in config; this command exists only
                # for the hosted kind, and quietly doing something else would be
                # a surprise about where a credential just went.
                output.fail(
                    "`secrets put` stores a HOSTED secret, so it needs --cloud.",
                    "Local references go in `secrets:` in config.yaml — "
                    "`andromeda secrets example` prints a starter block.",
                )
                return 2
            return secrets_cmd.put_cloud(args.name, args.value)
        if args.secrets_command == "list":
            return secrets_cmd.list_cloud()
        if args.secrets_command == "forget":
            return secrets_cmd.forget_cloud(args.name)
        if args.secrets_command == "schemes":
            return secrets_cmd.schemes()
        if args.secrets_command == "example":
            return secrets_cmd.example()
        return secrets_cmd.status()

    if args.command == "mcp":
        if args.mcp_command == "example":
            return mcp_cmd.example()
        if args.mcp_command == "login":
            return mcp_cmd.login(args.server)
        if args.mcp_command == "logout":
            return mcp_cmd.logout(args.server)
        return mcp_cmd.status()

    if args.command == "browser":
        if args.browser_command == "install":
            return browser_cmd.install()
        return browser_cmd.status()

    if args.command == "sessions":
        if args.sessions_command == "show":
            return sessions_cmd.show(args.id, live_only=args.live_only)
        if args.sessions_command == "search":
            return sessions_cmd.find(
                args.query,
                limit=args.limit,
                role=args.role,
                since=args.since,
                until=args.until,
                workspace=args.workspace,
                model=args.model,
                provider=args.provider,
            )
        if args.sessions_command == "recap":
            return sessions_cmd.recap(args.id)
        if args.sessions_command == "export":
            return sessions_cmd.export(args.id, args.fmt, args.out)
        if args.sessions_command == "rm":
            return sessions_cmd.remove(args.id, force=args.force)
        if args.sessions_command == "reindex":
            return sessions_cmd.reindex(force=args.force)
        if args.sessions_command == "doctor":
            return sessions_cmd.doctor()
        if args.sessions_command == "recover":
            return sessions_cmd.recover(
                apply=args.apply, rebuild=args.rebuild_index
            )
        if args.sessions_command == "active":
            return sessions_cmd.active()
        if args.sessions_command == "list":
            return sessions_cmd.show_list(
                limit=args.limit,
                since=args.since,
                until=args.until,
                workspace=args.workspace,
                model=args.model,
                provider=args.provider,
            )
        return sessions_cmd.show_list()

    if args.command == "memory":
        if args.memory_command == "search":
            return memory_cmd.find(args.query, limit=args.limit)
        if args.memory_command == "remember":
            return memory_cmd.remember(args.content, args.scope, args.tags)
        if args.memory_command == "forget":
            return memory_cmd.forget(args.query, args.scope, force=args.force)
        if args.memory_command == "export":
            return memory_cmd.export(args.path)
        if args.memory_command == "stats":
            return memory_cmd.stats()
        if args.memory_command == "list":
            return memory_cmd.show_list(args.scope)
        return memory_cmd.show_list()

    if args.command == "profile":
        if args.profile_command == "create":
            return profile_cmd.create(
                args.name, clone=args.clone, clone_all=args.clone_all
            )
        if args.profile_command == "use":
            return profile_cmd.use(args.name)
        if args.profile_command == "delete":
            return profile_cmd.delete(args.name, force=args.force)
        if args.profile_command == "current":
            return profile_cmd.current()
        return profile_cmd.show_list()

    if args.command == "model":
        if args.id:
            return config_cmd.set_value("model", args.id)
        return config_cmd.show("model")

    if args.config_command == "get":
        return config_cmd.show(args.key)
    if args.config_command == "set":
        return config_cmd.set_value(args.key, args.value)
    return config_cmd.where()


def _read_pipe(prompt: str | None) -> str | None:
    """Fold piped stdin into the prompt.

    Joined rather than substituted: `git log | andromeda "summarise this"` needs
    both halves, and dropping either one silently answers a different question.
    """
    try:
        if sys.stdin.isatty():
            return prompt
        piped = sys.stdin.read().strip()
    except (OSError, ValueError):
        # stdin can be closed, detached, or replaced by something that refuses
        # to be read (a launcher, a test harness). None of that is a reason to
        # fail a run that already has a prompt.
        return prompt

    if not piped:
        return prompt
    return f"{prompt}\n\n{piped}" if prompt else piped


def _take_profile(argv: list[str]) -> tuple[list[str], str]:
    """Pull `-p NAME` / `--profile NAME` out of argv before anything reads it.

    Handled here rather than as an argparse argument because it decides
    *which home directory this process has*, and both the verb dispatch and
    `config.load()` below have already resolved that by the time a parser
    runs. Accepted anywhere in the line, so `andromeda sessions -p work
    search x` works the way people type it.

    Returns the remaining argv and the profile name, so a bad name can be
    reported by the caller rather than by a crash in a directory lookup.
    """
    remaining: list[str] = []
    name = ""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-p", "--profile"}:
            if index + 1 >= len(argv):
                # Left in place, so argparse produces the error rather than
                # this silently dropping a flag somebody typed.
                remaining.append(token)
                index += 1
                continue
            name = argv[index + 1]
            index += 2
            continue
        if token.startswith("--profile="):
            name = token.split("=", 1)[1]
            index += 1
            continue
        remaining.append(token)
        index += 1
    return remaining, name


def _apply_secrets(*, warn_literals: bool = True) -> None:
    """Resolve the `secrets:` block, and never stop the run over it.

    A locked vault at eight in the morning is a reason to say which command
    unlocks it, not a reason `andromeda auth status` cannot run. Everything
    that does not need that credential still works, and the one thing that does
    fails with its own message when it is reached.

    `warn_literals` is off for `andromeda secrets` itself, which says the same
    thing better and in place. The startup warning exists for the people who
    never run that command; saying it twice to the person who just did is how a
    warning becomes something to scroll past.
    """
    from andromeda_agent import secrets as secrets_module

    try:
        config = config_module.load()
    except config_module.ConfigError:
        # Reported by whichever path reads the config next, with the file name
        # and the parse error. Saying it twice, differently, is worse.
        return

    # Said at startup and not only when someone runs `andromeda secrets`: a
    # pasted credential here is a plaintext key in a file documented as safe to
    # print and to commit, and it stays one until somebody is told.
    for name in secrets_module.literal_values(config) if warn_literals else ():
        output.fail(
            f"secrets.{name} is a value, not a reference — it is sitting in "
            f"{config_module.config_path()} in plain text.",
            "Move it into a vault. `andromeda secrets example`",
        )

    mapping = secrets_module.from_config(config)
    if not mapping:
        return

    for failure in secrets_module.apply(mapping).failures:
        output.fail(
            f"{failure.name}: could not read "
            f"{secrets_module.safe_reference(failure.reference)}",
            failure.remedy,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    argv, profile_name = _take_profile(argv)
    if profile_name:
        from . import profiles

        try:
            resolved = profiles.validate(profile_name)
        except profiles.ProfileError as exc:
            output.fail(str(exc))
            return 2
        if resolved != profiles.DEFAULT and not profiles.exists(resolved):
            output.fail(
                f"No profile {resolved!r}.",
                f"andromeda profile create {resolved}",
            )
            return 2
        # Set in the environment rather than passed down, because everything
        # that resolves a path — config, sessions, the index, the scheduler —
        # asks `config.home()`, and threading a profile through every one of
        # them is a change that would be forgotten in exactly one place.
        os.environ[profiles.ENV_PROFILE] = resolved

    # Vault-backed credentials, before anything reads the environment — which
    # means before the provider is built, before an MCP server's `env` block is
    # expanded, and before the first `terminal` call inherits it.
    #
    # Here rather than inside `config.load()` for two reasons: `load()` is
    # called many times in a run and has to stay a file read, and the resolvers
    # live in the agent package, which already imports this one.
    _apply_secrets(warn_literals=not (argv and argv[0] == "secrets"))

    if argv and argv[0] in COMMANDS:
        try:
            return _run_command(argv)
        except config_module.ConfigError as exc:
            output.fail(str(exc))
            return 2

    args = build_parser().parse_args(argv)

    try:
        config = _config(args)
    except config_module.ConfigError as exc:
        output.fail(str(exc))
        return 2

    # Before any surface opens, because a hook on `on_session_start` has to be
    # registered before the session starts. On a terminal this is where the
    # one-time approval prompt appears; with no terminal and no opt-in nothing
    # registers at all, which is the point.
    from andromeda_agent import shell_hooks

    shell_hooks.register_from_config(
        config, accept_hooks=getattr(args, "accept_hooks", False)
    )

    resumed = None
    if args.resume or args.continue_last:
        resumed = (
            sessions_store.resolve(args.resume)
            if args.resume
            else sessions_store.latest()
        )
        if resumed is None:
            output.fail(
                f"No session matching {args.resume!r}."
                if args.resume
                else "No saved sessions to continue.",
                "andromeda sessions",
            )
            return 2

    prompt = _read_pipe(args.prompt)

    if prompt:
        # `--tui` and a prompt are a contradiction: a one-shot has no screen to
        # take over, and a pipe must stay plain text. Refused rather than
        # ignored, for the same reason as `--resume` below — a flag that does
        # nothing teaches people the flag does not matter.
        if args.interface == "tui":
            output.fail(
                "--tui has no effect on a one-shot or a pipe.",
                'Drop the prompt to open the full-screen interface.',
            )
            return 2
        # Resuming into a one-shot is not wired: the transcript would grow from
        # a surface nobody is watching. Say so rather than silently ignoring it.
        if resumed is not None:
            output.fail(
                "--resume and --continue only apply to the interactive REPL.",
                "Drop the prompt to resume, or drop the flag to run one turn.",
            )
            return 2
        return chat.one_shot(prompt, config, workspace_root=args.workspace)

    if not sys.stdout.isatty():
        output.fail(
            "No prompt given and stdout is not a terminal.",
            'Pass a prompt: andromeda "your question"',
        )
        return 2

    if config.get("interface") == "tui":
        # Imported here, not at module scope: it pulls in Textual, and a
        # `andromeda config get` on an install without it must still work.
        from andromeda_tui import run as run_tui

        return run_tui(config, workspace_root=args.workspace, resume=resumed)

    return repl.run(config, workspace_root=args.workspace, resume=resumed)


if __name__ == "__main__":
    raise SystemExit(main())
