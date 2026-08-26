"""`andromeda status` — what this install is set to, and what it has spent.

The question people actually ask is not "what is in my config". It is some
mixture of "am I signed in", "which model is this", "how much have I used",
"why is it slow", and "is this session going to work here". Answering those one
`config get` at a time is what makes a CLI feel like a set of parts, so this is
one screen.

Two things it deliberately does not do.

**It makes no network call.** Everything here is read from disk: the config,
the credentials file, the session transcripts, `PATH`. A status command that
hangs because the network is down is a status command people stop running at
exactly the moment they need it.

**It reports no money at all.** Tokens are counted locally and reported as
tokens. There is no price table here and there must never be one — a local rate
that has drifted produces a cost figure somebody plans against. What the
account has left is the server's to state, and `andromeda auth` is where it is
asked. See `andromeda_agent.usage`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from andromeda_agent import lsp, project
from andromeda_agent import usage as usage_module

from .. import config as config_module
from .. import output
from .. import sessions as sessions_module

# How far back "recent" reaches. A week is the window in which somebody
# remembers what they were doing and can act on the number.
RECENT_DAYS = 7


def run(days: int = RECENT_DAYS) -> int:
    config = config_module.load()

    _install(config)
    output.console.print()
    _spend(days)
    output.console.print()
    _here(config)
    return 0


def _install(config: dict) -> None:
    provider = str(config.get("provider") or "")
    output.console.print("[bold]Install[/bold]")
    output.console.print(f"  model       {config.get('model')}")
    output.console.print(
        f"  lane        {provider}"
        + (
            "  [dim]hosted — Andromeda Credits[/dim]"
            if provider == "relay"
            else "  [dim]your own key, billed by the provider[/dim]"
        )
    )
    output.console.print(f"  thinking    {config.get('thinking')}")
    output.console.print(
        f"  approvals   {config.get('approval_mode')}"
        f"  [dim]ceiling {config.get('max_tier')}[/dim]"
    )
    output.console.print(f"  home        {config_module.home()}")

    if provider == "relay":
        signed_in = config_module.load_credentials().paired
        output.console.print(
            "  account     "
            + (
                "[green]signed in[/green]"
                if signed_in
                else "[red]not signed in[/red] [dim]— andromeda auth login[/dim]"
            )
        )
    else:
        variable = str(config.get("direct_api_key_env") or "")
        present = bool(os.environ.get(variable))
        output.console.print(
            f"  key         {variable} "
            + ("[green]set[/green]" if present else "[red]not set[/red]")
        )


def _spend(days: int) -> None:
    """Tokens, from the transcripts themselves.

    Read from the session files rather than from the index: the index is
    derived and may have been deleted, and a usage report that silently halves
    after a `sessions reindex` is worse than a slow one.
    """
    output.console.print(f"[bold]Usage[/bold] [dim]last {days} days[/dim]")

    cutoff = time.time() - days * 86_400
    totals = usage_module.Usage()
    sessions = 0
    for record in _recent(cutoff):
        entry = usage_module.Usage.from_dict(record.usage)
        if entry.empty:
            continue
        totals.merge(entry)
        sessions += 1

    if totals.empty:
        output.console.print(
            "  [dim]nothing recorded yet — usage is counted from the provider's"
            " own reply, so it starts at your next turn[/dim]"
        )
        return

    plural = "" if sessions == 1 else "s"
    output.console.print(
        f"  requests    {totals.requests:,}  [dim]across {sessions} session{plural}[/dim]"
    )
    line = (
        f"  tokens      {usage_module.compact(totals.total)}"
        f"  [dim]{usage_module.compact(totals.input)} in,"
        f" {usage_module.compact(totals.output)} out[/dim]"
    )
    output.console.print(line)
    if totals.cached:
        share = totals.cached / totals.input if totals.input else 0
        output.console.print(
            f"  cached      {usage_module.compact(totals.cached)}"
            f"  [dim]{share:.0%} of input served from cache[/dim]"
        )
    if totals.reasoning:
        output.console.print(
            f"  reasoning   {usage_module.compact(totals.reasoning)}"
            f"  [dim]of the output tokens[/dim]"
        )

    if len(totals.by_model) > 1:
        output.console.print()
        for model, counts in sorted(
            totals.by_model.items(),
            key=lambda pair: pair[1]["input"] + pair[1]["output"],
            reverse=True,
        ):
            spent = counts["input"] + counts["output"]
            output.console.print(
                f"    [cyan]{model}[/cyan] [dim]{usage_module.compact(spent)} "
                f"in {counts['requests']} request(s)[/dim]"
            )


def _recent(cutoff: float):
    """Sessions touched since `cutoff`, newest first, skipping unreadable ones."""
    directory = sessions_module.sessions_dir()
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        record = sessions_module.load(path.stem)
        if record is not None:
            records.append(record)
    records.sort(key=lambda record: record.updated_at, reverse=True)
    return records


def _here(config: dict) -> None:
    """What this directory means for the next session started in it."""
    output.console.print("[bold]Here[/bold]")
    posture = project.resolve(cwd=Path.cwd(), config=config, model=config.get("model"))
    workspace = posture.workspace

    if workspace is None:
        output.console.print("  [dim]not a workspace — no project context, no diagnostics[/dim]")
        return

    output.console.print(f"  root        {workspace.root}")
    output.console.print(
        "  posture     "
        + ("coding" if posture.is_coding else "general")
        + f"  [dim]coding_context={posture.mode}[/dim]"
    )

    facts = project.detect_facts(workspace.root)
    if facts.verify_commands:
        output.console.print(f"  verify      {'; '.join(facts.verify_commands)}")
    if facts.context_files:
        output.console.print(f"  context     {', '.join(facts.context_files)}")

    if not bool(config.get("lsp", True)):
        output.console.print("  diagnostics [dim]off[/dim]")
        return
    applicable = lsp.relevant(workspace.root)
    available = [entry for entry in applicable if entry.available]
    if available:
        names = ", ".join(entry.server.id for entry in available[:4])
        output.console.print(f"  diagnostics {names}")
    elif applicable:
        missing = ", ".join(entry.server.id for entry in applicable[:3])
        output.console.print(
            f"  diagnostics [dim]none installed ({missing}) — andromeda lsp status[/dim]"
        )
    else:
        output.console.print(
            "  diagnostics [dim]no server covers the languages here[/dim]"
        )
