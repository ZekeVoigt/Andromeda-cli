"""`andromeda acp` — this agent, inside an editor.

Thin by design. Everything protocol-shaped lives in `andromeda_agent.acp`;
what belongs here is building a real conversation for a working directory, and
the one rule that makes the transport work: **nothing may print to stdout.**
"""

from __future__ import annotations

import sys
from typing import Any

from andromeda_agent import acp as acp_module
from andromeda_agent import build_provider
from andromeda_agent.errors import AgentError

from .. import config as config_module
from .. import output, render
from ..session import build_conversation


def silence_stdout() -> None:
    """Point every console this program owns at stderr.

    stdout is the protocol. Rich writes there by default, and one stray line —
    a progress note, a warning, a banner — is a corrupt frame that takes the
    editor's session with it. stderr still reaches the editor's log, which is
    where a person looks when something is wrong.
    """
    for console in (render.console, render.err_console, output.console):
        console.file = sys.stderr


def run() -> int:
    config = config_module.load()

    try:
        provider = build_provider(config)
    except AgentError as exc:
        print(f"andromeda acp: {exc}", file=sys.stderr)
        return 1

    silence_stdout()

    def build(cwd: str) -> Any:
        conversation, _record = build_conversation(
            config,
            provider,
            interactive=True,
            workspace_root=cwd,
            surface="acp",
        )
        return conversation

    from andromeda_cli import __version__

    return acp_module.serve(build, version=__version__)
