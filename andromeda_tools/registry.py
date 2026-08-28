"""The tool registry.

Names and JSON schemas live here. Where a name also exists in the TypeScript
runtime's registry it must match it exactly — `tests/test_registry.py` pins the
overlap so the two cannot drift apart silently. The local tools below have no
counterpart there; the hosted runtime has no user filesystem to reach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from . import browser as browser_module, clarify as clarify_module, files
from . import mcp as mcp_module
from . import scheduling
from . import session_search as session_search_module
from . import processes as processes_module
from . import vision as vision_module
from . import skills as skills_module, terminal, web
from .memory import DEFAULT_LIMIT, DEFAULT_MIN_SCORE, MemoryStore
from .spec import ToolResult, ToolSpec
from .todo import TodoList
from .workspace import Workspace

# Enabled unless the user says otherwise. Read tools first, so that a session
# started with everything off still leaves the agent able to look.
DEFAULT_ENABLED = (
    "read_file",
    "list_dir",
    "search_files",
    "write_file",
    "patch",
    "terminal",
    "todo",
    "process",
    "clarify",
    "skill_load",
    "memory_search",
    "memory_store",
    "memory_forget",
    "session_search",
    "web_fetch",
    "web_search",
    "vision_analyze",
    "delegate",
    "subagents_list",
    "subagents_status",
    "subagents_wait",
    "browser_navigate",
    "browser_snapshot",
    "browser_read",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_back",
    "cron",
    # On by default, and it has to be. The whole failure it fixes is the agent
    # not knowing an app *could* be connected — a tool the person must first
    # discover and switch on cannot fix a discovery problem. It is still
    # `outbound`, so it stops for approval before it does anything.
    "connect_app",
    # `notepad` is deliberately absent: it only exists inside a scheduled run,
    # bound to that job, and `build_registry` only creates it when one is
    # passed. Listing it here would advertise a tool that no interactive
    # session has.
)


def build_registry(
    workspace: Workspace,
    todos: TodoList,
    skills: dict[str, skills_module.Skill] | None = None,
    memory: MemoryStore | None = None,
    delegate: ToolSpec | None = None,
    lane_tools: list[ToolSpec] | None = None,
    browser: browser_module.BrowserSession | None = None,
    processes: processes_module.ProcessRegistry | None = None,
    asker: clarify_module.Asker | None = None,
    mcp_servers: list[mcp_module.MCPServer] | None = None,
    vision: object | None = None,
    allow_private_network: bool = False,
    schedule: object | None = None,
    notepad: object | None = None,
    job_id: str = "",
    session_id: str = "",
    skills_home: "Path | None" = None,
    connect_home: "Path | None" = None,
    on_connected: "Callable[[], list[str]] | None" = None,
) -> dict[str, ToolSpec]:
    """Bind the tool functions to this session's workspace and state."""

    skills = {} if skills is None else skills

    specs: list[ToolSpec] = [
        ToolSpec(
            name="read_file",
            description=(
                "Read a text file from the workspace. Returns numbered lines. "
                "Use offset and limit for a file too large to read whole."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root, or absolute.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "0-based line to start from.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many lines to read.",
                    },
                },
                "required": ["path"],
            },
            risk_tier="safe_local",
            category="read",
            run=lambda path, offset=0, limit=files.MAX_READ_LINES: files.read_file(
                workspace, path, offset, limit
            ),
            summarize=files.arguments_summary_read,
        ),
        ToolSpec(
            name="list_dir",
            description="List the entries of a directory in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list."}
                },
                "required": [],
            },
            risk_tier="safe_local",
            category="read",
            run=lambda path=".": files.list_dir(workspace, path),
            summarize=lambda arguments: f"list {arguments.get('path', '.')}",
        ),
        ToolSpec(
            name="search_files",
            description=(
                "Search the workspace for a regular expression. Returns "
                "path:line: match. Skips dotfiles, node_modules and binaries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string", "description": "Directory to search under."},
                    "glob": {
                        "type": "string",
                        "description": "Filename glob, e.g. *.py. Defaults to everything.",
                    },
                },
                "required": ["pattern"],
            },
            risk_tier="safe_local",
            category="read",
            run=lambda pattern, path=".", glob="*": files.search_files(
                workspace, pattern, path, glob
            ),
            summarize=lambda arguments: f"search {arguments.get('pattern', '')!r}",
        ),
        ToolSpec(
            name="write_file",
            description=(
                "Write a file, creating parent directories as needed. "
                "Overwrites the whole file — use patch to change part of one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            risk_tier="destructive",
            category="write",
            run=lambda path, content: files.write_file(workspace, path, content),
            summarize=files.arguments_summary_write,
        ),
        ToolSpec(
            name="patch",
            description=(
                "Replace an exact string in a file. old_string must match "
                "exactly, including whitespace, and must be unique unless "
                "replace_all is set."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            risk_tier="destructive",
            category="write",
            run=lambda path, old_string, new_string, replace_all=False: files.patch(
                workspace, path, old_string, new_string, replace_all
            ),
            summarize=files.arguments_summary_patch,
        ),
        ToolSpec(
            name="terminal",
            description=(
                "Run a shell command in the workspace and return its output. "
                "Non-zero exits are returned, not raised."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "integer",
                        "description": f"Seconds, max {terminal.MAX_TIMEOUT}.",
                    },
                    "cwd": {"type": "string", "description": "Directory to run in."},
                    "background": {
                        "type": "boolean",
                        "description": (
                            "Start the command and return at once, leaving it "
                            "running. Use for servers, watchers and builds you "
                            "want to keep working alongside. Poll it with the "
                            "`process` tool."
                        ),
                    },
                },
                "required": ["command"],
            },
            risk_tier="destructive",
            category="admin",
            run=lambda command, timeout=terminal.DEFAULT_TIMEOUT, cwd=None, background=False: (
                terminal.run_command(workspace, command, timeout, cwd, background, processes)
            ),
            summarize=terminal.arguments_summary,
        ),
        ToolSpec(
            name="todo",
            description=(
                "Replace the task list for this session. Send the whole list "
                "every time. At most one task may be in_progress."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done"],
                                },
                            },
                            "required": ["task", "status"],
                        },
                    }
                },
                "required": ["items"],
            },
            risk_tier="safe_local",
            category="write",
            run=lambda items: todos.replace(items),
            summarize=lambda arguments: f"todo ({len(arguments.get('items') or [])} items)",
        ),
    ]

    # ---- names shared with the TypeScript registry -----------------------
    # Schemas below are mirrored from `lib/agent-runtime/tools/definitions.ts`
    # verbatim. Two harnesses that grade or shape the same tool differently is
    # how a person learns that what they taught one surface means nothing to
    # the other. `tests/test_registry_drift.py` records each pair as deliberate.

    specs.append(
        ToolSpec(
            name="skill_load",
            description=(
                "Load the instructions for one skill from the session's skill "
                "manifest. Call this before following a skill; skill bodies are "
                "not preloaded. Optionally load one supporting resource from "
                "that skill after its main instructions point to it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name from the Skills manifest.",
                    },
                    "resource": {
                        "type": "string",
                        "description": (
                            "Optional path inside the skill directory, such as "
                            "references/api.md. Omit to load the main skill "
                            "instructions."
                        ),
                    },
                },
                "required": ["name"],
            },
            risk_tier="safe_local",
            category="read",
            run=lambda name, resource=None: skills_module.load_skill(
                skills, name, resource, home=skills_home
            ),
            summarize=lambda arguments: f"skill {arguments.get('name', '?')}",
        )
    )

    if memory is not None:
        specs.extend(_memory_specs(memory))

    # Unconditional: it binds to no session state, only to the index for
    # whichever profile this process is using. Read and `safe_local`, so a
    # read-only lane can check whether something was already discussed —
    # which is the case it exists for.
    specs.append(
        ToolSpec(
            name="session_search",
            description=session_search_module.DESCRIPTION,
            parameters=session_search_module.SCHEMA,
            risk_tier="safe_local",
            category="read",
            run=session_search_module.run,
            summarize=session_search_module.summarize,
        )
    )

    if processes is not None:
        specs.append(
            ToolSpec(
                name="process",
                description=(
                    "Manage background processes started with terminal(background=true). "
                    "Actions: 'list' (show all), 'poll' (status plus output since the "
                    "last poll), 'log' (output with pagination), 'wait' (block until it "
                    "exits or the timeout), 'kill' (terminate the whole process tree), "
                    "'write' (send raw stdin without a newline), 'submit' (send data "
                    "plus Enter, for answering prompts), 'close' (close stdin)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "list", "poll", "log", "wait", "kill",
                                "write", "submit", "close",
                            ],
                            "description": "Action to perform on background processes",
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Process session ID from the terminal background output. "
                                "Required for every action except 'list'. A unique prefix "
                                "works too."
                            ),
                        },
                        "data": {
                            "type": "string",
                            "description": "Text to send to stdin, for 'write' and 'submit'.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max seconds to block for 'wait'.",
                            "minimum": 1,
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Line offset for 'log' (default: the last 200).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max lines to return for 'log'.",
                            "minimum": 1,
                        },
                    },
                    "required": ["action"],
                },
                # Killing a process and writing to its stdin are both real
                # actions on the machine, and `list`/`poll` are not worth a
                # second tool at a lower tier.
                risk_tier="destructive",
                category="admin",
                run=lambda action, session_id="", data="", timeout=processes_module.DEFAULT_WAIT_SECONDS, offset=None, limit=None: (
                    processes_module.act(
                        processes, action, session_id, data, timeout, offset, limit
                    )
                ),
                summarize=lambda arguments: (
                    f"process {arguments.get('action', '?')}"
                    + (f" {arguments['session_id']}" if arguments.get("session_id") else "")
                ),
            )
        )

    # Registered even without an asker: the model should be able to try, and
    # be told plainly that nobody is there, rather than never learning the
    # option existed. The refusal reads as guidance, not as a broken tool.
    specs.append(
        ToolSpec(
            name="clarify",
            description=clarify_module.DESCRIPTION,
            parameters=clarify_module.PARAMETERS,
            risk_tier="safe_local",
            category="read",
            run=lambda question="", choices=None, questions=None: clarify_module.ask(
                asker, question, choices, questions
            ),
            summarize=lambda arguments: (
                "ask: " + str(arguments.get("question") or "several questions")[:80]
            ),
        )
    )

    if vision is not None:
        specs.append(
            ToolSpec(
                name="vision_analyze",
                description=(
                    "Read an image and describe it. Use for images that are "
                    "themselves the subject — a design mock, a chart, a photo, a "
                    "screenshot someone sent you. Do NOT use it to read a web "
                    "page: browser_snapshot gives you the page's real structure "
                    "with refs you can act on, which is both cheaper and correct. "
                    "This call goes to a separate, more expensive model."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Image file in the workspace. PNG, JPEG, GIF or WebP.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "What you need to know about it. Omit for a full "
                                "description."
                            ),
                        },
                    },
                    "required": ["path"],
                },
                # Reads a file and spends money at a higher rate than the
                # conversation model. `outbound` rather than `safe_local` so the
                # cost is a deliberate choice in `ask` mode.
                risk_tier="outbound",
                category="read",
                run=lambda path, prompt="": vision_module.analyze(
                    workspace, vision, path, prompt
                ),
                summarize=lambda arguments: f"look at {arguments.get('path', '?')}",
            )
        )

    specs.append(
        ToolSpec(
            name="web_fetch",
            description="Fetch content from a URL and return it as readable text.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
            risk_tier="safe_local",
            category="read",
            run=lambda url: web.fetch(url, allow_private_network),
            summarize=lambda arguments: f"fetch {arguments.get('url', '')}",
        )
    )

    # Registered only when a provider key is present. Advertising a capability
    # that can only ever answer "not configured" wastes a turn and teaches the
    # model to keep asking.
    if web.configured_provider():
        specs.append(
            ToolSpec(
                name="web_search",
                description="Search the web for current information.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "number", "description": "Max results (default 5)"},
                    },
                    "required": ["query"],
                },
                risk_tier="safe_local",
                category="read",
                run=lambda query, limit=web.DEFAULT_RESULTS: web.search(query, limit),
                summarize=lambda arguments: f"search {arguments.get('query', '')!r}",
            )
        )

    # Registered only when Playwright is installed. Offering a browser that
    # cannot open is the same mistake as offering a search with no key.
    if browser is not None and browser_module.playwright_available():
        specs.extend(_browser_specs(browser, allow_private_network))

    # MCP tools carry their server's name, so they never collide with a local
    # tool or with each other. Added before delegation so a lane can hold them.
    for server in mcp_servers or []:
        specs.extend(mcp_module.specs_for(server))

    # Only where there is a person to approve it and a home to write to. A
    # delegated lane gets neither: connecting an app writes the config that
    # decides what *every future session* reaches, which is exactly the kind of
    # thing a context spawned out of the person's sight must not do — the same
    # rule that keeps `delegate` and `schedule` out of a lane.
    if connect_home is not None:
        from . import connect as connect_module

        specs.append(connect_module.spec(connect_home, on_connected))

    if schedule is not None:
        # Only where there is a schedule to write to. An interactive session
        # gets one; a delegated lane does not, for the same reason it does not
        # get `delegate` — a context spawned out of the person's sight must not
        # be able to create one that outlives it.
        specs.append(
            scheduling.cron_spec(schedule, str(workspace.root), session_id=session_id)
        )

    if notepad is not None and job_id:
        specs.append(scheduling.notepad_spec(notepad, job_id))

    if delegate is not None:
        specs.append(delegate)
        # Only alongside `delegate`: a lane cannot start one, so a lane has no
        # lanes to inspect, and offering it the pair would be offering it a
        # view of its siblings it must not have.
        specs.extend(lane_tools or [])

    built = {spec.name: spec for spec in specs}

    # Plugin tools last, and by assignment rather than by appending, so an
    # override actually replaces the built-in instead of losing to it in the
    # dict comprehension above. Whether a plugin was allowed to claim a
    # built-in's name was already decided at registration, under the
    # `tools.override` capability — by the time a spec reaches here the answer
    # is yes.
    for spec in _plugin_specs():
        built[spec.name] = spec

    return built


def _plugin_specs() -> list[ToolSpec]:
    """Tools registered by plugins, or nothing.

    Imported inside the function on purpose. `andromeda_agent.plugins` reaches
    back into this package for `ToolSpec` and for the built-in name list, and a
    module-level import here would close that cycle at interpreter start.
    """
    try:
        from andromeda_agent import plugins as plugins_module
    except ImportError:  # pragma: no cover - only if the package is half-installed
        return []
    return plugins_module.plugin_tool_specs()


def _browser_specs(
    session: browser_module.BrowserSession, allow_private_network: bool = False
) -> list[ToolSpec]:
    """The `browser_*` family.

    Every one of these is `destructive`. Not because a page read changes this
    machine — it does not — but because a browser carries the user's signed-in
    sessions, and "click this ref" is how an agent sends an email, files an
    order or deletes an account. Tiering the read tools lower would be true of
    the read and false of the surface, and the surface is what the gate is
    protecting.
    """
    ref_property = {
        "type": "string",
        "description": "Element ref from the most recent snapshot, e.g. e3.",
    }
    include_text = {
        "type": "boolean",
        "description": "Also return the page's readable text. Off by default.",
    }

    return [
        ToolSpec(
            name="browser_navigate",
            description=(
                "Open a URL in the agent's browser and return a snapshot of the "
                "page: its title, heading, and every interactive element with a "
                "ref. Act on refs — there are no screenshots and no coordinates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open."},
                    "include_text": include_text,
                },
                "required": ["url"],
            },
            risk_tier="destructive",
            category="write",
            run=lambda url, include_text=False: browser_module.navigate(
                session, url, include_text, allow_private_network
            ),
            summarize=lambda arguments: f"browse {arguments.get('url', '')}",
        ),
        ToolSpec(
            name="browser_snapshot",
            description=(
                "Re-read the current page: title, heading, and every interactive "
                "element with a fresh ref. Refs from earlier snapshots go stale "
                "whenever the page changes, so take a new one if a ref is missing."
            ),
            parameters={
                "type": "object",
                "properties": {"include_text": include_text},
                "required": [],
            },
            risk_tier="destructive",
            category="write",
            run=lambda include_text=False: browser_module.snapshot(session, include_text),
            summarize=lambda _arguments: "snapshot the page",
        ),
        ToolSpec(
            name="browser_read",
            description="Return the current page's readable text, without the element outline.",
            parameters={"type": "object", "properties": {}, "required": []},
            risk_tier="destructive",
            category="write",
            run=lambda: browser_module.read_page(session),
            summarize=lambda _arguments: "read the page text",
        ),
        ToolSpec(
            name="browser_click",
            description="Click the element with this ref, then return a fresh snapshot.",
            parameters={
                "type": "object",
                "properties": {"ref": ref_property},
                "required": ["ref"],
            },
            risk_tier="destructive",
            category="write",
            run=lambda ref: browser_module.click(session, ref),
            summarize=lambda arguments: f"click {arguments.get('ref', '?')}",
        ),
        ToolSpec(
            name="browser_type",
            description=(
                "Type into the field with this ref, replacing what is there. "
                "Set submit to press Enter afterwards."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref": ref_property,
                    "text": {"type": "string", "description": "Text to type."},
                    "submit": {
                        "type": "boolean",
                        "description": "Press Enter after typing. Defaults to false.",
                    },
                },
                "required": ["ref", "text"],
            },
            risk_tier="destructive",
            category="write",
            run=lambda ref, text, submit=False: browser_module.type_text(
                session, ref, text, submit
            ),
            summarize=lambda arguments: (
                f"type into {arguments.get('ref', '?')}: "
                f"{str(arguments.get('text', ''))[:60]}"
            ),
        ),
        ToolSpec(
            name="browser_press",
            description="Press a key on the current page, e.g. Enter, Escape, Tab.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key name."}},
                "required": ["key"],
            },
            risk_tier="destructive",
            category="write",
            run=lambda key: browser_module.press(session, key),
            summarize=lambda arguments: f"press {arguments.get('key', '?')}",
        ),
        ToolSpec(
            name="browser_scroll",
            description="Scroll the page up, down, to the top or to the bottom.",
            parameters={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "top", "bottom"],
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Screens to scroll, for up and down. Defaults to 1.",
                    },
                },
                "required": [],
            },
            risk_tier="destructive",
            category="write",
            run=lambda direction="down", amount=1: browser_module.scroll(
                session, direction, amount
            ),
            summarize=lambda arguments: f"scroll {arguments.get('direction', 'down')}",
        ),
        ToolSpec(
            name="browser_back",
            description="Go back one page in history, then return a fresh snapshot.",
            parameters={"type": "object", "properties": {}, "required": []},
            risk_tier="destructive",
            category="write",
            run=lambda: browser_module.back(session),
            summarize=lambda _arguments: "go back",
        ),
    ]


def _memory_specs(memory: MemoryStore) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="memory_search",
            description=(
                "Search your memory for relevant information from past "
                "conversations, documents, or learned context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "limit": {"type": "number", "description": "Max results (default 5)"},
                    "minScore": {
                        "type": "number",
                        "description": (
                            "Minimum match score from 0 to 1. Defaults to 0.3."
                        ),
                    },
                },
                "required": ["query"],
            },
            risk_tier="safe_local",
            category="read",
            run=lambda query, limit=DEFAULT_LIMIT, minScore=DEFAULT_MIN_SCORE: memory.search(  # noqa: N803
                query, limit, minScore
            ),
            summarize=lambda arguments: f"recall {arguments.get('query', '')!r}",
        ),
        ToolSpec(
            name="memory_store",
            description=(
                "Remember something durable about this person or their work. "
                "Write one self-contained fact per call, in your own words, "
                "phrased so it still makes sense months from now with no "
                "surrounding conversation. Save proactively — you do not need to "
                "be asked. Good candidates: how they want to be addressed, "
                "preferences and working style, corrections they make to you, "
                "standing constraints, people and projects that recur, decisions "
                "and why. Skip: anything you can look up, one-off task details, "
                "and things that will be false next week. Restating something you "
                "already know is safe — writes are consolidated, so it reinforces "
                "rather than duplicates."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "One fact, as a complete sentence with its own context."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["standing", "episode"],
                        "description": (
                            "'standing' for things that define who they are and how "
                            "you work with them. These load on every message, so the "
                            "bar is high and the space is small. 'episode' for "
                            "everything else. Defaults to episode."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Rough kind: preference, identity, instruction, person, "
                            "project, decision, lesson."
                        ),
                    },
                    "replaces": {
                        "type": "string",
                        "description": (
                            "What this makes untrue, described as you would recall "
                            "it. Matching memories are removed as this one is written."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional path or source label for the memory.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags to help retrieve this later.",
                    },
                },
                "required": ["content"],
            },
            # `safe_local`, matching TOOL_RISK_TIERS in the TypeScript
            # registry. A write that supersedes is a write that deletes, which
            # argues for a higher tier — but the hosted side decided this one
            # deliberately, and a memory the agent can capture on one surface
            # and not the other is the exact failure the shared-name rule
            # exists to prevent. Gating it here would also mean no memory
            # capture at all in a pipe, where the ceiling drops to safe_local.
            risk_tier="safe_local",
            category="write",
            run=lambda content, scope="episode", category=None, replaces=None, path=None, tags=None: memory.store(
                content, scope, category, replaces, path, tags
            ),
            summarize=lambda arguments: f"remember: {str(arguments.get('content', ''))[:90]}",
        ),
        ToolSpec(
            name="memory_forget",
            description=(
                "Remove something you remember that is wrong, outdated, or that "
                "the person asked you to drop. Use this when a stored memory is no "
                "longer true and no replacement fact covers it — if you are simply "
                "learning a newer version of the same thing, call memory_store "
                "instead and the older one is superseded automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to forget, described the way you would recall it. "
                            "Matching memories are removed."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["standing", "episode", "any"],
                        "description": "Which tier to forget from. Defaults to any.",
                    },
                },
                "required": ["query"],
            },
            risk_tier="safe_local",
            category="write",
            run=lambda query, scope="any": memory.forget(query, scope),
            summarize=lambda arguments: f"forget {arguments.get('query', '')!r}",
        ),
    ]


def openai_schemas(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    return [spec.to_openai() for spec in specs]


__all__ = [
    "DEFAULT_ENABLED",
    "ToolResult",
    "ToolSpec",
    "build_registry",
    "openai_schemas",
]
