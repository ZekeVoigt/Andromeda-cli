"""Assembling a conversation from configuration.

One place, so the REPL and the one-shot path cannot end up with different
policies for the same settings — which is the kind of divergence that makes
"it asked me last time" a real bug report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from andromeda_agent import Callbacks, Conversation, Policy
from andromeda_agent.allowlist import Allowlist
from andromeda_agent.delegation import (
    Delegation,
    build_brief,
    make_delegate_tool,
    make_lane_tools,
)
from andromeda_agent.lanes import LaneRegistry
from andromeda_agent import auxiliary as auxiliary_module
from andromeda_agent import compaction as compaction_module
from andromeda_agent.models import context_window
from andromeda_agent import soul
from andromeda_agent.schedule import Schedule
from andromeda_agent.specialists import SPECIALISTS
from andromeda_tools import (
    BrowserSession,
    MemoryStore,
    Workspace,
    build_registry,
    skills as skills_module,
)
from andromeda_tools import mcp as mcp_module
from andromeda_tools.processes import ProcessRegistry
from andromeda_tools.todo import TodoList

from . import config as config_module
from . import sessions as sessions_module
from . import state


def _window(config: dict[str, Any], provider) -> int:
    """The context window to compact against.

    An explicit setting wins — it is the only way to test compaction without a
    million-token conversation — otherwise it comes from the model.
    """
    configured = int(config.get("context_window") or 0)
    return configured if configured > 0 else context_window(provider.model)


def build_allowlist() -> Allowlist:
    return Allowlist(config_module.home() / "approvals.json")


def schedule_path() -> Path:
    return config_module.home() / "cron" / "cron.json"


def notepad_path() -> Path:
    return config_module.home() / "cron" / "notepad.json"


def build_policy(
    config: dict[str, Any], *, interactive: bool, allowlist: Allowlist | None = None
) -> Policy:
    """Turn settings into a gate.

    `interactive` is not a setting the user writes — it is a fact about the
    surface. A non-interactive run has nobody to answer a prompt, so its policy
    is narrowed rather than left to fail one call at a time.
    """
    policy = Policy(
        mode=config["approval_mode"],
        enabled=frozenset(config["enabled_tools"]),
        max_tier=config["max_tier"],
        # Learned trust only where someone taught it: a non-interactive run
        # never asked, so it has nothing to inherit and no way to add to it.
        allowlist=allowlist if interactive else None,
    )
    if not interactive and policy.mode == "ask":
        # `ask` with no one to ask means every gated tool is refused mid-turn.
        # Narrowing the ceiling instead means the model is never offered them,
        # so it plans around what it actually has. `--approval auto` is the
        # explicit way to grant more.
        policy = policy.narrow(max_tier="safe_local")
    return policy


def build_conversation(
    config: dict[str, Any],
    provider,
    *,
    interactive: bool,
    workspace_root: str | None = None,
    session: "sessions_module.Session | None" = None,
    notepad: Any = None,
    job_id: str = "",
) -> tuple[Conversation, "sessions_module.Session"]:
    workspace = Workspace(workspace_root)
    todos = TodoList()

    found_skills = skills_module.discover(workspace.root)
    memory = MemoryStore(
        config_module.home() / "memory", config.get("memory_backend")
    )
    # One browser for the session, shared with every lane. It is not started
    # until something navigates, so a session that never browses pays nothing.
    browser = BrowserSession()
    lanes = LaneRegistry()
    processes = ProcessRegistry()
    # Connected eagerly at session start rather than on first use: a tool the
    # model is never told about is a tool it never calls, and a server that
    # takes two seconds to start would otherwise cost that on the first call
    # of every session instead of once.
    vision = auxiliary_module.build("vision", provider)
    mcp_servers = mcp_module.build_servers(config_module.home())
    for server in mcp_servers:
        server.connect()

    if notepad is not None and job_id:
        # Enabled here rather than added to `DEFAULT_ENABLED`, because no
        # interactive session has a notepad and a default that names a tool
        # nothing provides is a default that lies. Through the config so the
        # policy is still built by `build_policy` and nothing widens a policy
        # after the fact — `Policy.narrow` is deliberately the only derivation
        # path, and this is not a derivation.
        config = {**config, "enabled_tools": [*config["enabled_tools"], "notepad"]}

    allowlist = build_allowlist() if interactive else None
    policy = build_policy(config, interactive=interactive, allowlist=allowlist)

    def child_registry(specialist_id: str) -> dict:
        """The lane's toolbelt.

        Built without `delegate`, unconditionally. Every specialist has
        `can_spawn=False` and `is_session_tool` would deny it anyway — this is
        the third guard, and it is here because depth is the one limit whose
        failure mode is unbounded rather than merely wrong.
        """
        return build_registry(
            workspace,
            TodoList(),
            found_skills,
            memory,
            delegate=None,
            browser=browser,
            processes=processes,
            mcp_servers=mcp_servers,
            vision=vision,
            # A lane has nobody watching it: the person is reading the parent's
            # turn, not the lane's. Its `clarify` refuses, and says why.
            asker=None,
            allow_private_network=bool(config["allow_private_network"]),
        )

    def run_lane(
        specialist: str,
        task: str,
        context: str,
        success_criteria: list[str],
        expected_output: str,
        allowed_tools: list[str] | None,
        denied_tools: list[str] | None,
        label: str,
        lane=None,
    ) -> Delegation:
        belt = SPECIALISTS[specialist]
        registry = child_registry(specialist)

        # Derived from the parent's policy, never constructed fresh: a lane can
        # only ever hold a subset of what this session holds. An `allowedTools`
        # naming something the parent lacks intersects away to nothing rather
        # than granting it.
        enabled = policy.enabled
        if allowed_tools:
            enabled = enabled & frozenset(allowed_tools)
        child_policy = policy.narrow(enabled=enabled, specialist=belt)
        if denied_tools:
            child_policy = child_policy.narrow(
                enabled=child_policy.enabled - frozenset(denied_tools)
            )

        child = Conversation(
            provider=provider,
            policy=child_policy,
            workspace=workspace,
            max_tokens=int(config["max_tokens"]),
            temperature=float(config["temperature"]),
            max_steps=belt.max_turns,
            context_window=_window(config, provider),
            system_prompt=build_brief(
                belt.id,
                task,
                context,
                success_criteria,
                expected_output,
                toolbelt=[
                    spec.name
                    for spec in registry.values()
                    if child_policy.decide(spec) != "denied"
                ],
            ),
            registry=registry,
            # No persistence and no rebuild hook: a lane is not a session, and
            # its transcript belongs to the parent's turn, not to the store.
        )
        # The brief IS the system prompt, so the first user message only has to
        # start it working.
        used: list[str] = []

        def note(spec, _arguments) -> None:
            used.append(spec.name)
            # Progress, so a long lane is not mistaken for a stalled one.
            if lane is not None:
                lanes.note_progress(lane, spec.name)

        report = child.send("Begin.", Callbacks(on_tool_start=note))
        return Delegation(
            specialist=belt.id,
            label=label or belt.label,
            report=report,
            turns=child.steps_taken,
            tools_used=used,
        )

    # The scheduler this session may write to. Interactive only: a job created
    # from a pipe would be created by nobody in particular and would outlive
    # the run that made it, and a lane gets none for the same reason it gets no
    # `delegate` — a context spawned out of the person's sight must not create
    # one that outlives it.
    schedule = Schedule(schedule_path()) if interactive else None

    def rebuild(fresh_todos: TodoList):
        return build_registry(
            workspace,
            fresh_todos,
            found_skills,
            memory,
            delegate=make_delegate_tool(run_lane, on_start=_announce, registry=lanes),
            lane_tools=make_lane_tools(lanes),
            browser=browser,
            processes=processes,
            asker=_ask_user,
            mcp_servers=mcp_servers,
            vision=vision,
            allow_private_network=bool(config["allow_private_network"]),
            schedule=schedule,
            # Only a scheduled run passes these, and it passes both. The
            # notepad is bound to one job, so a registry with the tool and no
            # job to write to would be a tool that always errors.
            notepad=notepad,
            job_id=job_id,
        )

    record = session or sessions_module.Session()
    record.provider = provider.name
    record.model = provider.model
    record.workspace = str(workspace.root)

    # The transcript this conversation writes to, held indirectly so a surface
    # can point it somewhere else mid-run without rebuilding the registry, the
    # policy or the provider. Those are properties of *this terminal*; the
    # transcript is not, and conflating them is why switching sessions used to
    # mean starting a new process.
    binding = sessions_module.Binding(record)

    def persist(messages: list[dict[str, Any]]) -> None:
        binding.record.messages = messages
        binding.record.save()
        # Indexed on the same schedule the transcript is written, so a session
        # is searchable the moment it exists rather than after some later
        # sweep. Best-effort by contract: `index_session` swallows its own
        # storage errors, because failing to index is a failure to search
        # later and must never become a failure to answer now.
        state.index_session(binding.record)

    def archive(messages: list[dict[str, Any]], first: int, last: int) -> str:
        """Keep the turns compaction is about to discard, and say where.

        Order matters and is the whole correctness argument: the transcript is
        saved and indexed *first*, so the rows exist to be marked; only then
        are they archived. Doing it the other way round would archive rows that
        were never written, and the summary would promise a lookup that finds
        nothing.

        An archived row is the only remaining copy — the turns leave the
        transcript on disk too — which is why `write_session` never deletes
        one on a rebuild.
        """
        record = binding.record
        record.messages = messages
        record.save()
        state.index_session(record)
        archived = state.archive_range(record.id, first, last)
        # Keyed on what was actually archived, not on what was asked for. A
        # note promising searchable turns when nothing was stored is the one
        # failure mode worth guarding here.
        return compaction_module.recall_note(record.id, archived)

    conversation = Conversation(
        provider=provider,
        policy=policy,
        workspace=workspace,
        max_tokens=int(config["max_tokens"]),
        temperature=float(config["temperature"]),
        context_window=_window(config, provider),
        todos=todos,
        registry=rebuild(todos),
        context_blocks=_context_blocks(found_skills, memory),
        on_persist=persist,
        on_archive=archive,
        rebuild_registry=rebuild,
    )

    # Attached so the surface can report lanes and processes still running when
    # a turn ends, and clean them up when the session does.
    conversation.lane_registry = lanes
    conversation.process_registry = processes
    conversation.mcp_servers = mcp_servers
    conversation.binding = binding

    # A resumed session replaces the transcript wholesale, including its
    # original system message — rewriting it would silently change the rules
    # the earlier turns were produced under.
    if session is not None and session.messages:
        conversation.messages = list(session.messages)

    return conversation, record


_announce_hook: Any = None


def set_lane_announcer(hook) -> None:
    """How the surface hears that a lane started. Set by the REPL and one-shot."""
    global _announce_hook
    _announce_hook = hook


def _announce(specialist_id: str, label: str) -> None:
    if _announce_hook is not None:
        _announce_hook(specialist_id, label)


_asker_hook: Any = None


def set_asker(hook) -> None:
    """How the surface puts a question to the person. Set by the REPL only."""
    global _asker_hook
    _asker_hook = hook


def _ask_user(questions):
    if _asker_hook is None:
        raise RuntimeError("no asker is attached to this surface")
    return _asker_hook(questions)


def _context_blocks(
    found_skills: dict[str, skills_module.Skill], memory: MemoryStore
) -> list[str]:
    # Where the agent's own state lives. Asked often enough — "where are my
    # sessions", "what have you remembered" — and a guess is worse than a fact.
    home = config_module.home()
    blocks: list[str] = [
        "Your own state on this machine:\n"
        f"  config    {config_module.config_path()}\n"
        f"  sessions  {home / 'sessions'}\n"
        f"  memory    {memory.file}\n"
        f"  index     {state.db_path()}"
    ]

    # The user's own standing instructions go first among the context blocks,
    # ahead of skills and memories: they are the only block a person wrote by
    # hand, and when they conflict with something this program inferred, the
    # person wins.
    soul_block = soul.block(home)
    if soul_block:
        blocks.append(soul_block)

    manifest = skills_module.manifest(found_skills)
    if manifest:
        blocks.append(manifest)

    standing = memory.standing()
    if standing:
        # Standing memories only. Episodes are recalled through memory_search
        # when they are relevant; loading them all would put every fact the
        # agent has ever learned into every prompt.
        lines = "\n".join(f"  - {item.content}" for item in standing)
        blocks.append(f"What you already know about this person:\n{lines}")

    return blocks
