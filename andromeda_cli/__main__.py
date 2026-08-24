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

import sys

from . import bootstrap

bootstrap.install()

import argparse  # noqa: E402

from . import __version__  # noqa: E402
from . import config as config_module  # noqa: E402
from . import output, repl  # noqa: E402
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
    mcp_cmd,
    service,
    transfer,
    sessions as sessions_cmd,
    tools,
    update as update_cmd,
)
from . import sessions as sessions_store  # noqa: E402

COMMANDS = (
    "setup",
    "auth",
    "cron",
    "eval",
    "mcp",
    "approvals",
    "backup",
    "export",
    "restore",
    "config",
    "tools",
    "model",
    "sessions",
    "browser",
    "update",
    "doctor",
)

EPILOG = """
examples:
  andromeda                          start the REPL
  andromeda --tui                    start the full-screen interface
  andromeda "what is 2+2"            one turn, then exit
  git log -5 | andromeda "summarise" read the pipe as part of the prompt

commands:
  andromeda auth login <code>        pair this machine with an account
  andromeda auth status | logout
  andromeda config get [key]
  andromeda config set <key> <value>
  andromeda config path
  andromeda tools                    list tools and how each is gated
  andromeda tools enable|disable <name>
  andromeda model [id]               show or set the model
  --thinking off|low|medium|high     how hard the model thinks
  andromeda sessions                 recent sessions
  andromeda sessions show <id>
  andromeda sessions search <text>
  andromeda --resume <id>            pick a session back up
  andromeda --continue               pick the most recent one back up
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

    auth_parser = sub.add_parser("auth", help="Pair or unpair this machine.")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", help="Pair using a code from the app.")
    login.add_argument("code", help="The pairing code shown in the app.")
    auth_sub.add_parser("status", help="Show whether this machine is paired.")
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
    sub.add_parser("doctor", help="Show what is and is not working.")
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
        help="Only scenarios whose name contains this. `list` to list them.",
    )
    eval_parser.add_argument("--json", action="store_true", dest="as_json")
    eval_parser.add_argument("--root", help="Directory of scenarios.")

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
        "--approval", choices=["ask", "auto", "deny"], required=True
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

    cron_sub.add_parser("install", help="Run the scheduler in the background, at login.")
    cron_sub.add_parser("uninstall", help="Remove the background scheduler.")
    cron_sub.add_parser("service", help="Whether the background scheduler is installed.")

    approvals_parser = sub.add_parser("approvals", help="Learned approvals.")
    approvals_sub = approvals_parser.add_subparsers(dest="approvals_command")
    forget = approvals_sub.add_parser("forget", help="Ask about this tool again.")
    forget.add_argument("tool")
    approvals_sub.add_parser("clear", help="Forget every learned approval.")

    mcp_parser = sub.add_parser("mcp", help="Configured MCP servers.")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_sub.add_parser("example", help="Print a starter mcp.json.")

    browser_parser = sub.add_parser("browser", help="The browser tools.")
    browser_sub = browser_parser.add_subparsers(dest="browser_command")
    browser_sub.add_parser("install", help="Install Playwright and Chromium.")
    browser_sub.add_parser("status", help="Show whether the browser tools are usable.")

    sessions_parser = sub.add_parser("sessions", help="Past sessions.")
    sessions_sub = sessions_parser.add_subparsers(dest="sessions_command")
    show = sessions_sub.add_parser("show", help="Print one session's transcript.")
    show.add_argument("id")
    find = sessions_sub.add_parser("search", help="Find sessions containing text.")
    find.add_argument("query")

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
            return auth.login(args.code, base_url=str(config_module.load()["base_url"]))
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

    if args.command == "doctor":
        return doctor.run()

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
        return evals.run(pattern=args.pattern, as_json=args.as_json, root=args.root)

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
            return cron.approve(args.id, args.approval)
        if args.cron_command == "notepad":
            return cron.notepad(args.id, args.action, args.key, args.value)
        if args.cron_command == "daemon":
            return cron.daemon(once=args.once)
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
        return approvals.show()

    if args.command == "mcp":
        if args.mcp_command == "example":
            return mcp_cmd.example()
        return mcp_cmd.status()

    if args.command == "browser":
        if args.browser_command == "install":
            return browser_cmd.install()
        return browser_cmd.status()

    if args.command == "sessions":
        if args.sessions_command == "show":
            return sessions_cmd.show(args.id)
        if args.sessions_command == "search":
            return sessions_cmd.find(args.query)
        return sessions_cmd.show_list()

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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

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
