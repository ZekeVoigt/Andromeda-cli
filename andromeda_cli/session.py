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
from andromeda_agent import plugins as plugins_module
from andromeda_agent import compaction as compaction_module
from andromeda_agent import curator as curator_module
from andromeda_agent import hints as hints_module
from andromeda_agent import hooks
from andromeda_agent import lsp as lsp_module
from andromeda_agent import project as project_module
from andromeda_agent import worktrees
from andromeda_agent.models import context_window
from andromeda_agent import soul
from andromeda_agent import usage as usage_module
from andromeda_agent.schedule import Schedule
from andromeda_agent.specialists import SPECIALISTS
from andromeda_tools import (
    BrowserSession,
    MemoryStore,
    Workspace,
    build_registry,
    skills as skills_module,
)
from andromeda_tools import browser as browser_module
from andromeda_tools import mcp as mcp_module
from andromeda_tools import skill_scan
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
    surface: str = "",
) -> tuple[Conversation, "sessions_module.Session"]:
    workspace = Workspace(workspace_root)
    todos = TodoList()

    # Resolved once, here, and never re-probed: every block it produces sits in
    # the cached prefix of every request this session sends, and re-running
    # `git status` per turn would rewrite that prefix for a line that is stale
    # by the time the model reads it anyway.
    posture = project_module.resolve(
        cwd=workspace.root, config=config, model=provider.model
    )

    # Scanned before they are offered. A skill is instructions that go into the
    # model's context and files it may open, and the ones in a workspace
    # arrived with whatever was cloned there — see `skill_scan`. What the scan
    # blocks is kept in `withheld_skills` rather than dropped, so the surface
    # can say a skill exists and is not being used.
    found_skills = skills_module.discover(workspace.root)

    # A week's arithmetic over the agent's own skills, run before they are
    # offered rather than after: sweeping afterwards would list a skill in this
    # session's prompt and archive it out from under the same session.
    curator_note = _curate(config, found_skills)
    if curator_note:
        found_skills = skills_module.discover(workspace.root)

    screened = skill_scan.screen(
        found_skills, config_module.home(), skills_module.bundled_skills_dir()
    )
    withheld_skills = {
        name: screened[name]
        for name in list(found_skills)
        if not skill_scan.is_allowed(screened[name])
    }
    for name in withheld_skills:
        found_skills.pop(name, None)
    memory = MemoryStore(
        config_module.home() / "memory", config.get("memory_backend")
    )
    # One browser for the session, shared with every lane. It is not started
    # until something navigates, so a session that never browses pays nothing.
    browser = browser_module.build_session()
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

    # Diagnostics after an edit. Built only inside a workspace: outside one
    # there is no project root to start a server in, and starting one against
    # `$HOME` would index the user's entire home directory the first time the
    # model touched a Python file.
    lsp_severities = lsp_module.parse_severities(config.get("lsp_severities"))
    lsp_service = (
        lsp_module.Service(posture.workspace.root, severities=lsp_severities)
        if posture.workspace is not None and bool(config.get("lsp", True))
        else None
    )

    def child_registry(specialist_id: str, lane_workspace: Workspace | None = None) -> dict:
        """The lane's toolbelt.

        Built without `delegate`, unconditionally. Every specialist has
        `can_spawn=False` and `is_session_tool` would deny it anyway — this is
        the third guard, and it is here because depth is the one limit whose
        failure mode is unbounded rather than merely wrong.

        `lane_workspace` is the lane's own git worktree when isolation is on.
        The tools are bound to it, so the confinement check is what keeps a
        lane out of the main checkout — not a sentence in its brief.
        """
        return build_registry(
            lane_workspace or workspace,
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
            skills_home=config_module.home(),
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

        # One working copy per lane, when the setting is on and this is a git
        # repository. Everything about it degrades to the shared tree rather
        # than failing: isolation is an improvement on the default, never a
        # precondition for delegating.
        worktree = None
        if config.get("worktree_isolation"):
            worktree = worktrees.create(workspace.root, lane.id if lane is not None else "")
        lane_workspace = Workspace(worktree.path) if worktree is not None else None

        registry = child_registry(specialist, lane_workspace)

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
            workspace=lane_workspace or workspace,
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
            )
            + (worktrees.brief_note(worktree) if worktree is not None else ""),
            # The lane gets the posture too, resolved against *its* tree — an
            # isolated lane is a different checkout with a different branch and
            # a different dirty state, and handing it the parent's snapshot
            # would describe a directory it is not working in. Tailored to the
            # belt, so a read-only specialist is never told to edit with
            # `patch`, and never given the coding brief at all when its belt
            # cannot change a file.
            context_blocks=project_module.resolve(
                cwd=(lane_workspace or workspace).root,
                config=config,
                model=provider.model,
            ).blocks(
                {
                    spec.name
                    for spec in registry.values()
                    if child_policy.decide(spec) != "denied"
                }
            ),
            registry=registry,
            tool_search_mode=str(config.get("tool_search") or "auto"),
            tool_search_listing_tokens=int(
                config.get("tool_search_listing_tokens") or 0
            ),
            # A lane's tool calls are part of the parent's session, so they
            # report the parent's id — a hook that saw a fresh id per lane
            # could not tell a delegated call from a new conversation.
            session_id=binding.record.id,
            surface="lane",
            # No persistence and no rebuild hook: a lane is not a session, and
            # its transcript belongs to the parent's turn, not to the store.
        )
        # A writing lane gets diagnostics too. Read-only belts get none: a
        # lane that cannot edit can never introduce a diagnostic, and starting
        # an indexer for it is pure cost.
        #
        # An isolated lane gets its own service, because its worktree is a
        # different checkout on a different branch — a server rooted in the
        # parent's tree would answer about files the lane is not editing. A
        # lane sharing the working directory shares the parent's service, so
        # one tree never runs two copies of the same indexer.
        lane_lsp = None
        if lsp_service is not None and any(
            spec.name in lsp_module.EDIT_TOOLS
            and child_policy.decide(spec) != "denied"
            for spec in registry.values()
        ):
            lane_lsp = (
                lsp_module.Service(worktree.path, severities=lsp_severities)
                if worktree is not None
                else lsp_service
            )
        child.lsp = lane_lsp

        # The brief IS the system prompt, so the first user message only has to
        # start it working.
        used: list[str] = []

        def note(spec, _arguments) -> None:
            used.append(spec.name)
            # Progress, so a long lane is not mistaken for a stalled one.
            if lane is not None:
                lanes.note_progress(lane, spec.name)

        try:
            report = child.send("Begin.", Callbacks(on_tool_start=note))
        finally:
            # A service the lane owns dies with the lane. The parent's is left
            # alone — it belongs to the session, not to this delegation.
            if lane_lsp is not None and lane_lsp is not lsp_service:
                lane_lsp.stop()
            # In `finally`, because a lane that raised is exactly the one whose
            # half-finished work must not be swept away. `finalize` prunes only
            # on proof that there is nothing there.
            outcome = worktrees.finalize(worktree) if worktree is not None else None

        return Delegation(
            specialist=belt.id,
            label=label or belt.label,
            report=report,
            turns=child.steps_taken,
            tools_used=used,
            worktree=outcome,
        )

    # The scheduler this session may write to. Interactive only: a job created
    # from a pipe would be created by nobody in particular and would outlive
    # the run that made it, and a lane gets none for the same reason it gets no
    # `delegate` — a context spawned out of the person's sight must not create
    # one that outlives it.
    schedule = Schedule(schedule_path()) if interactive else None

    # Filled in once the conversation exists — `rebuild` is defined before it,
    # and the reload needs to reach back into it. A one-element list rather
    # than a `nonlocal`, so the closure below reads the *current* value rather
    # than the one bound at definition.
    live: list = []

    def reconnect_mcp() -> list[str]:
        """Pick up servers connected since this session started.

        Mutates `mcp_servers` in place rather than rebinding it: that list is
        what `rebuild` closes over, and a fresh list would leave the rebuild
        still looking at the old one.
        """
        known = {server.name for server in mcp_servers}
        for server in mcp_module.build_servers(config_module.home()):
            if server.name in known:
                continue
            server.connect()
            mcp_servers.append(server)
        if not live:
            return []
        return live[0].reload_tools()

    def rebuild(fresh_todos: TodoList):
        return build_registry(
            workspace,
            fresh_todos,
            found_skills,
            memory,
            delegate=make_delegate_tool(
                run_lane,
                on_start=_announce,
                registry=lanes,
                session_id=binding.record.id,
                isolated=bool(config.get("worktree_isolation")),
            ),
            lane_tools=make_lane_tools(lanes),
            browser=browser,
            processes=processes,
            asker=_ask_user,
            mcp_servers=mcp_servers,
            vision=vision,
            allow_private_network=bool(config["allow_private_network"]),
            skills_home=config_module.home(),
            connect_home=config_module.home(),
            on_connected=reconnect_mcp,
            schedule=schedule,
            # Which conversation this is, so a job the agent creates is bound
            # to it and reports back into it. Interactive only, by the same
            # rule as `schedule` above.
            session_id=binding.record.id if interactive else "",
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
        # Written on the same schedule as the transcript, because a token count
        # that is only saved at a clean exit is a token count that is missing
        # from every session that crashed — which are the expensive ones.
        binding.record.usage = conversation.usage.as_dict()
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
        record.usage = conversation.usage.as_dict()
        record.save()
        state.index_session(record)
        archived = state.archive_range(record.id, first, last)
        # Keyed on what was actually archived, not on what was asked for. A
        # note promising searchable turns when nothing was stored is the one
        # failure mode worth guarding here.
        return compaction_module.recall_note(record.id, archived)

    # Built here rather than inline, because the coding brief has to name only
    # the tools this session actually offers — and "offers" means the registry
    # minus whatever the policy would refuse outright, which is the same set
    # `Conversation.available` hands the model.
    registry = rebuild(todos)
    offered = {
        name for name, spec in registry.items() if policy.decide(spec) != "denied"
    }

    conversation = Conversation(
        provider=provider,
        policy=policy,
        workspace=workspace,
        max_tokens=int(config["max_tokens"]),
        temperature=float(config["temperature"]),
        context_window=_window(config, provider),
        todos=todos,
        registry=registry,
        context_blocks=_context_blocks(found_skills, memory, posture, offered),
        on_persist=persist,
        on_archive=archive,
        rebuild_registry=rebuild,
        tool_search_mode=str(config.get("tool_search") or "auto"),
        tool_search_listing_tokens=int(config.get("tool_search_listing_tokens") or 0),
        session_id=record.id,
        # Named for the hook payloads. Derived rather than required, so a
        # caller that never heard of surfaces still reports something true.
        surface=surface or ("repl" if interactive else "once"),
    )

    conversation.lsp = lsp_service

    # Discovers a package's own AGENTS.md when the model first reads something
    # under it. Seeded with what the prompt already carries, or the first tool
    # call re-delivers the file the model has been reading all session. Only
    # inside a workspace: elsewhere there is no tree to confine it to, and an
    # unconfined tracker is one that reads another agent's house rules out of
    # the home directory.
    if posture.workspace is not None and posture.mode != "off":
        tracker = hints_module.Hints(
            workspace.root, boundary=posture.workspace.chain_root
        )
        tracker.seed_from_workspace(posture.workspace)
        conversation.hints = tracker

    # So `/skills` can say what was found and not offered. A capability that
    # vanishes with no explanation is one people work around by turning the
    # whole feature off.
    conversation.withheld_skills = withheld_skills
    conversation.curator_note = curator_note

    # Attached so the surface can report lanes and processes still running when
    # a turn ends, and clean them up when the session does.
    conversation.lane_registry = lanes
    conversation.process_registry = processes
    conversation.mcp_servers = mcp_servers
    # Closes the loop for `reconnect_mcp` above, which cannot name the
    # conversation because it is defined before there is one.
    live.append(conversation)
    conversation.binding = binding

    # A resumed session replaces the transcript wholesale, including its
    # original system message — rewriting it would silently change the rules
    # the earlier turns were produced under.
    if session is not None and session.messages:
        conversation.messages = list(session.messages)

    # A resumed session keeps what it already spent. Starting the count again
    # at zero would make `andromeda status` report the last sitting rather than
    # the session, and a long-running session is exactly the one worth
    # measuring.
    if session is not None and session.usage:
        conversation.usage = usage_module.Usage.from_dict(session.usage)

    # Lent to the plugins before the session-start hook fires, so a plugin
    # that reaches for `ctx.dispatch_tool` from `on_session_start` finds a
    # registry rather than the honest refusal it would otherwise get. Here for
    # the same reason the hook is: every way into a conversation comes through
    # this function.
    plugins_module.bind_session(conversation.registry, vision)

    # Fired here rather than in each surface: every way into a conversation
    # comes through this function, and a lifecycle event that four call sites
    # have to remember to fire is one that three of them will forget.
    hooks.fire(
        "on_session_start",
        session_id=record.id,
        model=provider.model,
        surface=conversation.surface,
    )

    return conversation, record


def _curate(config: dict[str, Any], found_skills: dict[str, Any]) -> str:
    """Sweep the skill library, if it is time. Returns a line, or "".

    Best-effort by contract, like every other startup check: a session that
    could not tidy a directory is still a session, and failing to open over
    housekeeping would be the worst possible trade.
    """
    try:
        home = config_module.home()
        settings = curator_module.Settings.from_config(config)
        if not curator_module.due(home, settings):
            return ""
        result = curator_module.sweep(found_skills, home, settings)
        if not result.changed:
            return ""
        return f"curated skills — {result.summary()}"
    except Exception:  # noqa: BLE001 - housekeeping must never fail a session
        return ""


def ended(conversation, *, completed: bool = True) -> None:
    """Report that a session is over, from wherever it ended.

    Idempotent on purpose: a surface can reach here from its clean exit and
    from its exception path, and a lifecycle event that fires twice is worse
    than one that fires late — a hook counting sessions would silently
    double-count every crash.
    """
    if conversation is None or getattr(conversation, "_session_ended", False):
        return
    conversation._session_ended = True
    # Before the hook, not after: a shell hook is allowed to take its time, and
    # a language server left running past the end of a session is a `pyright`
    # somebody finds in `top` an hour later and cannot explain.
    service = getattr(conversation, "lsp", None)
    if service is not None:
        try:
            service.stop()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
    hooks.fire(
        "on_session_end",
        session_id=getattr(conversation, "session_id", ""),
        model=getattr(conversation.provider, "model", ""),
        surface=getattr(conversation, "surface", "repl"),
        turn_count=conversation.turn_count,
        completed=completed,
    )


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
    found_skills: dict[str, skills_module.Skill],
    memory: MemoryStore,
    posture: project_module.Posture | None = None,
    tool_names: set[str] | None = None,
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

    # After SOUL.md and before the skills manifest. The person's own file is
    # the one block a human wrote by hand and it stays first; the project's
    # conventions come next, because they are about the work rather than about
    # the worker; the manifest and the memories are inventories, and an
    # inventory read before the instructions is an inventory read without
    # knowing what to look for.
    if posture is not None:
        blocks.extend(posture.blocks(tool_names))

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
