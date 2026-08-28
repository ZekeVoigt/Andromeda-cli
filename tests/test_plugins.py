"""The plugin socket: manifests, discovery, load order, context, unload."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from andromeda_agent import hooks
from andromeda_agent import plugin_capabilities as caps
from andromeda_agent import plugin_store, plugins


@pytest.fixture(autouse=True)
def clean_state():
    plugins.reset()
    hooks.reset()
    yield
    plugins.reset()
    hooks.reset()


def write_plugin(
    root: Path,
    plugin_id: str,
    *,
    body: str = "def register(ctx):\n    pass\n",
    manifest: str | None = None,
) -> Path:
    """A plugin directory on disk. Returns its path."""
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(
        manifest
        if manifest is not None
        else f"name: {plugin_id}\nversion: 1.0.0\ndescription: test plugin\n",
        encoding="utf-8",
    )
    (directory / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


def load_one(directory: Path, source: str = "user"):
    """Read a manifest, mark it enabled, and load it. Returns the LoadedPlugin."""
    manifest = plugins.read_manifest(directory, source)
    plugin_store.update(manifest.id, enabled=True)
    plugins.load({manifest.id: manifest})
    return plugins.manager().loaded[manifest.id]


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------


def test_a_manifest_without_a_name_is_refused(tmp_path):
    """The name is the id — it keys the ledger and namespaces the state
    directory — so a plugin without one is not addressable at all."""
    (tmp_path / "plugin.yaml").write_text("version: 1.0.0\n", encoding="utf-8")
    with pytest.raises(plugins.PluginError, match="no `name:`"):
        plugins.read_manifest(tmp_path, "user")


@pytest.mark.parametrize("name", ["Has Space", "../escape", "a" * 65, "-leading"])
def test_a_name_that_cannot_be_a_path_segment_is_refused(tmp_path, name):
    """The id becomes a directory under plugin-data and an event prefix."""
    (tmp_path / "plugin.yaml").write_text(f"name: {name!r}\n", encoding="utf-8")
    with pytest.raises(plugins.PluginError, match="not usable as an id"):
        plugins.read_manifest(tmp_path, "user")


def test_an_unknown_kind_degrades_to_standalone(tmp_path, caplog):
    (tmp_path / "plugin.yaml").write_text(
        "name: thing\nkind: spaceship\n", encoding="utf-8"
    )
    manifest = plugins.read_manifest(tmp_path, "user")
    assert manifest.kind == "standalone"
    assert "spaceship" in caplog.text


def test_unknown_capabilities_are_carried_not_dropped(tmp_path):
    """A manifest written against a newer Andromeda still loads, and `show`
    can say which of its asks were not understood."""
    (tmp_path / "plugin.yaml").write_text(
        "name: thing\ncapabilities: [tools.override, quantum.tunnelling]\n",
        encoding="utf-8",
    )
    manifest = plugins.read_manifest(tmp_path, "user")
    assert manifest.capabilities == ("tools.override",)
    assert manifest.unknown_capabilities == ("quantum.tunnelling",)


def test_unknown_fields_are_reported(tmp_path):
    """`capabilties:` silently doing nothing is the failure this prevents."""
    (tmp_path / "plugin.yaml").write_text(
        "name: thing\ncapabilties: [tools.override]\n", encoding="utf-8"
    )
    manifest = plugins.read_manifest(tmp_path, "user")
    assert manifest.unknown_fields == ("capabilties",)


def test_an_author_table_is_flattened(tmp_path):
    (tmp_path / "plugin.yaml").write_text(
        "name: thing\nauthor:\n  name: Ada\n  email: ada@example.com\n",
        encoding="utf-8",
    )
    assert plugins.read_manifest(tmp_path, "user").author == "Ada, ada@example.com"


def test_malformed_yaml_names_the_file(tmp_path):
    (tmp_path / "plugin.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(plugins.PluginError, match="not valid YAML"):
        plugins.read_manifest(tmp_path, "user")


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_later_sources_win_a_name_collision(tmp_path, monkeypatch):
    """Dropping your own copy in the user directory replaces a bundled plugin
    with no config change — the whole reason the order is what it is."""
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    write_plugin(bundled, "shared", manifest="name: shared\nversion: 1.0.0\n")
    write_plugin(user, "shared", manifest="name: shared\nversion: 9.9.9\n")

    monkeypatch.setattr(plugins, "bundled_dir", lambda: bundled)
    monkeypatch.setattr(plugins, "user_dir", lambda: user)

    found = plugins.discover()
    assert found["shared"].version == "9.9.9"
    assert found["shared"].source == "user"


def test_project_plugins_are_ignored_without_the_opt_in(tmp_path, monkeypatch):
    """A repository you cloned must not put Python in your agent's process."""
    project = tmp_path / ".andromeda" / "plugins"
    write_plugin(project, "sneaky")

    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(plugins, "project_dir", lambda: project)
    monkeypatch.delenv(plugins.ENV_PROJECT_PLUGINS, raising=False)

    assert "sneaky" not in plugins.discover()

    monkeypatch.setenv(plugins.ENV_PROJECT_PLUGINS, "1")
    assert "sneaky" in plugins.discover()


def test_a_directory_without_a_manifest_is_not_a_plugin(tmp_path, monkeypatch):
    (tmp_path / "notaplugin").mkdir()
    (tmp_path / "notaplugin" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    assert plugins.discover() == {}


def test_a_broken_manifest_skips_only_itself(tmp_path, monkeypatch, caplog):
    write_plugin(tmp_path, "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("version: 1\n", encoding="utf-8")

    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)

    found = plugins.discover()
    assert set(found) == {"good"}
    assert "bad" in caplog.text


def test_the_bundled_directory_must_look_like_ours(tmp_path):
    """The walk passes through directories nobody meant it to see, so an
    unrelated `plugins/` two levels up must not be adopted."""
    empty = tmp_path / "plugins"
    empty.mkdir()
    assert plugins._looks_like_plugins_dir(empty) is False

    write_plugin(empty, "real")
    assert plugins._looks_like_plugins_dir(empty) is True


def test_the_disable_switch_stops_everything(tmp_path, monkeypatch):
    directory = write_plugin(tmp_path, "thing")
    manifest = plugins.read_manifest(directory, "user")
    plugin_store.update("thing", enabled=True)

    monkeypatch.setenv(plugins.ENV_DISABLE, "1")
    plugins.load({"thing": manifest})
    assert plugins.manager().loaded == {}


# ---------------------------------------------------------------------------
# load order
# ---------------------------------------------------------------------------


def _manifests(spec: dict[str, list[str]]) -> dict[str, plugins.PluginManifest]:
    return {
        key: plugins.PluginManifest(
            id=key,
            name=key,
            directory=Path("/nowhere"),
            source="user",
            requires_plugins=tuple(dependencies),
        )
        for key, dependencies in spec.items()
    }


def test_a_dependency_loads_first():
    order = plugins.resolve_load_order(_manifests({"a": ["b"], "b": [], "c": []}))
    assert order.index("b") < order.index("a")


def test_ties_break_alphabetically():
    """Same order on every machine, or a bug reproduces on one of them."""
    assert plugins.resolve_load_order(_manifests({"z": [], "a": [], "m": []})) == [
        "a",
        "m",
        "z",
    ]


def test_a_cycle_falls_back_rather_than_refusing_to_start(caplog):
    """Two third-party plugins referencing each other must not stop the agent."""
    order = plugins.resolve_load_order(_manifests({"a": ["b"], "b": ["a"]}))
    assert sorted(order) == ["a", "b"]
    assert "cycle" in caplog.text


def test_a_missing_dependency_warns_and_still_loads(caplog):
    order = plugins.resolve_load_order(_manifests({"a": ["absent"]}))
    assert order == ["a"]
    assert "absent" in caplog.text
    assert "has_plugin" in caplog.text


def test_a_self_dependency_is_ignored(caplog):
    assert plugins.resolve_load_order(_manifests({"a": ["a"]})) == ["a"]
    assert "depends on itself" in caplog.text


# ---------------------------------------------------------------------------
# loading and failure isolation
# ---------------------------------------------------------------------------


def test_register_is_called_with_a_context(tmp_path):
    directory = write_plugin(
        tmp_path,
        "greeter",
        body="""
        SEEN = []

        def register(ctx):
            SEEN.append(ctx.plugin_id)
        """,
    )
    entry = load_one(directory)
    assert entry.ok
    assert entry.module.SEEN == ["greeter"]


def test_a_plugin_that_raises_does_not_stop_the_others(tmp_path, monkeypatch):
    write_plugin(
        tmp_path,
        "broken",
        body="def register(ctx):\n    raise RuntimeError('nope')\n",
    )
    write_plugin(
        tmp_path,
        "working",
        body="""
        def register(ctx):
            ctx.register_command("ok", lambda raw: "fine")
        """,
    )
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    plugin_store.update("broken", enabled=True)
    plugin_store.update("working", enabled=True)

    plugins.load()

    assert "nope" in plugins.manager().loaded["broken"].error
    assert plugins.manager().loaded["working"].ok
    assert "ok" in plugins.plugin_commands()


def test_a_plugin_without_register_says_so(tmp_path):
    directory = write_plugin(tmp_path, "empty", body="VALUE = 1\n")
    entry = load_one(directory)
    assert "no `register(ctx)`" in entry.error


def test_an_import_error_is_reported_not_raised(tmp_path):
    directory = write_plugin(tmp_path, "bad", body="import a_module_that_is_not_real\n")
    entry = load_one(directory)
    assert "import failed" in entry.error


def test_a_missing_init_is_reported(tmp_path):
    directory = tmp_path / "manifestonly"
    directory.mkdir()
    (directory / "plugin.yaml").write_text("name: manifestonly\n", encoding="utf-8")
    entry = load_one(directory)
    assert "no __init__.py" in entry.error


def test_a_disabled_plugin_is_never_imported(tmp_path):
    """Consent happens before code runs. A plugin that is merely installed has
    not been given permission to execute one line."""
    directory = write_plugin(
        tmp_path,
        "eager",
        body="raise RuntimeError('this should never run')\n",
    )
    manifest = plugins.read_manifest(directory, "user")
    plugins.load({manifest.id: manifest})
    assert plugins.manager().loaded == {}


def test_a_newer_api_version_is_refused_before_import(tmp_path):
    directory = write_plugin(
        tmp_path,
        "future",
        manifest="name: future\napi_version: 99\n",
        body="raise RuntimeError('never imported')\n",
    )
    entry = load_one(directory)
    assert "api_version 99" in entry.error


def test_missing_required_env_refuses_before_import(tmp_path, monkeypatch):
    monkeypatch.delenv("PLUGIN_TEST_KEY", raising=False)
    directory = write_plugin(
        tmp_path,
        "needsenv",
        manifest="name: needsenv\nrequires_env: [PLUGIN_TEST_KEY]\n",
        body="raise RuntimeError('never imported')\n",
    )
    entry = load_one(directory)
    assert "PLUGIN_TEST_KEY" in entry.error


def test_an_ungranted_capability_refuses_before_import(tmp_path):
    """The gate has to be checked before the import, or the plugin has already
    run whatever it wanted by the time anyone asks."""
    directory = write_plugin(
        tmp_path,
        "greedy",
        manifest="name: greedy\ncapabilities: [tools.override]\n",
        body="raise RuntimeError('never imported')\n",
    )
    entry = load_one(directory)
    assert "not been granted" in entry.error
    assert "tools.override" in entry.error


# ---------------------------------------------------------------------------
# registering tools
# ---------------------------------------------------------------------------


def test_a_plugin_tool_reaches_the_registry(tmp_path):
    directory = write_plugin(
        tmp_path,
        "toolish",
        body="""
        def register(ctx):
            ctx.register_tool(
                "shout",
                "Say it louder.",
                {"type": "object", "properties": {}},
                lambda: "HI",
                risk_tier="safe_local",
                category="read",
            )
        """,
    )
    assert load_one(directory).ok
    specs = {spec.name: spec for spec in plugins.plugin_tool_specs()}
    assert specs["shout"].risk_tier == "safe_local"
    assert specs["shout"].run() == "HI"


def test_a_plugin_tool_defaults_to_the_pessimistic_tier(tmp_path):
    """An author who omits the tier gets a tool that asks first, not one that
    is silently treated as a safe local read."""
    directory = write_plugin(
        tmp_path,
        "lazy",
        body="""
        def register(ctx):
            ctx.register_tool("thing", "d", {}, lambda: "x")
        """,
    )
    assert load_one(directory).ok
    spec = plugins.plugin_tool_specs()[0]
    assert spec.risk_tier == "outbound"
    assert spec.category == "write"


def test_claiming_a_builtin_tool_name_is_refused(tmp_path):
    """Silently shadowing `terminal` is the worst thing this socket could
    allow by accident."""
    directory = write_plugin(
        tmp_path,
        "shadow",
        body="""
        def register(ctx):
            ctx.register_tool("terminal", "mine now", {}, lambda: "x")
        """,
    )
    entry = load_one(directory)
    assert "without asking to override" in entry.error
    assert plugins.plugin_tool_specs() == []


def test_overriding_a_builtin_needs_the_capability(tmp_path):
    directory = write_plugin(
        tmp_path,
        "shadow",
        body="""
        def register(ctx):
            ctx.register_tool("terminal", "mine", {}, lambda: "x", override=True)
        """,
    )
    entry = load_one(directory)
    assert "tools.override" in entry.error


def test_a_granted_override_replaces_the_builtin(tmp_path):
    directory = write_plugin(
        tmp_path,
        "shadow",
        manifest="name: shadow\ncapabilities: [tools.override]\n",
        body="""
        def register(ctx):
            ctx.register_tool(
                "terminal", "mine", {}, lambda **k: "intercepted", override=True
            )
        """,
    )
    caps.grant("shadow", ["tools.override"])
    assert load_one(directory).ok
    assert [spec.name for spec in plugins.plugin_tool_specs()] == ["terminal"]


def test_an_unknown_risk_tier_is_refused(tmp_path):
    directory = write_plugin(
        tmp_path,
        "wrong",
        body="""
        def register(ctx):
            ctx.register_tool("t", "d", {}, lambda: "x", risk_tier="spicy")
        """,
    )
    assert "unknown risk tier" in load_one(directory).error


def test_two_plugins_claiming_one_tool_name_keeps_the_first(tmp_path, monkeypatch, caplog):
    body = """
        def register(ctx):
            ctx.register_tool("shared", "%s", {}, lambda: "%s")
        """
    write_plugin(tmp_path, "alpha", body=body % ("from alpha", "alpha"))
    write_plugin(tmp_path, "beta", body=body % ("from beta", "beta"))
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    plugin_store.update("alpha", enabled=True)
    plugin_store.update("beta", enabled=True)

    plugins.load()

    specs = plugins.plugin_tool_specs()
    assert len(specs) == 1
    assert specs[0].run() == "alpha"
    assert "already provides" in caplog.text


# ---------------------------------------------------------------------------
# hooks and commands
# ---------------------------------------------------------------------------


def test_a_plugin_hook_fires(tmp_path):
    directory = write_plugin(
        tmp_path,
        "watcher",
        body="""
        SEEN = []

        def register(ctx):
            ctx.register_hook("post_tool_call", lambda **kw: SEEN.append(kw.get("tool_name")))
        """,
    )
    entry = load_one(directory)
    hooks.invoke_hook("post_tool_call", tool_name="read_file")
    assert entry.module.SEEN == ["read_file"]


def test_an_unknown_hook_event_is_refused(tmp_path):
    directory = write_plugin(
        tmp_path,
        "typo",
        body="""
        def register(ctx):
            ctx.register_hook("on_sesion_start", lambda **kw: None)
        """,
    )
    assert "unknown hook event" in load_one(directory).error


def test_claiming_a_builtin_command_is_refused(tmp_path):
    directory = write_plugin(
        tmp_path,
        "hijack",
        body="""
        def register(ctx):
            ctx.register_command("exit", lambda raw: "mine")
        """,
    )
    assert "without asking to override" in load_one(directory).error


def test_a_granted_command_override_is_marked(tmp_path):
    directory = write_plugin(
        tmp_path,
        "hijack",
        manifest="name: hijack\ncapabilities: [commands.override]\n",
        body="""
        def register(ctx):
            ctx.register_command("help", lambda raw: "mine", override=True)
        """,
    )
    caps.grant("hijack", ["commands.override"])
    assert load_one(directory).ok
    assert plugins.plugin_commands()["help"].override is True


def test_a_cli_command_is_registered(tmp_path):
    directory = write_plugin(
        tmp_path,
        "clier",
        body="""
        def register(ctx):
            ctx.register_cli_command(
                "greet", "Say hi.", lambda parser: None, lambda args: 0
            )
        """,
    )
    assert load_one(directory).ok
    assert plugins.plugin_cli_commands()["greet"].plugin_id == "clier"


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------


def test_a_plugin_skill_is_namespaced(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: thing\n---\nbody", encoding="utf-8")
    directory = write_plugin(
        tmp_path,
        "skiller",
        body=f"""
        from pathlib import Path

        def register(ctx):
            ctx.register_skill("deploy", Path({str(skill)!r}))
        """,
    )
    assert load_one(directory).ok
    assert "skiller:deploy" in plugins.plugin_skills()


def test_a_skill_name_with_a_colon_is_refused(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("body", encoding="utf-8")
    directory = write_plugin(
        tmp_path,
        "skiller",
        body=f"""
        from pathlib import Path

        def register(ctx):
            ctx.register_skill("other:deploy", Path({str(skill)!r}))
        """,
    )
    assert "invalid skill name" in load_one(directory).error


# ---------------------------------------------------------------------------
# prompt sections
# ---------------------------------------------------------------------------


def test_a_prompt_section_needs_the_capability(tmp_path):
    directory = write_plugin(
        tmp_path,
        "loud",
        body="""
        def register(ctx):
            ctx.register_system_prompt_section("rules", "Always be brief.")
        """,
    )
    assert "prompt.inject" in load_one(directory).error


def test_a_granted_prompt_section_renders_inside_markers(tmp_path):
    directory = write_plugin(
        tmp_path,
        "loud",
        manifest="name: loud\ncapabilities: [prompt.inject]\n",
        body="""
        def register(ctx):
            ctx.register_system_prompt_section("rules", "Always be brief.")
        """,
    )
    caps.grant("loud", ["prompt.inject"])
    assert load_one(directory).ok

    rendered = plugins.render_prompt_sections()
    assert rendered.startswith(plugins.PROMPT_SECTIONS_START)
    assert plugins.PROMPT_SECTIONS_END in rendered
    assert "Always be brief." in rendered
    assert "loud:rules" in rendered


def test_an_oversized_prompt_section_is_refused(tmp_path):
    directory = write_plugin(
        tmp_path,
        "loud",
        manifest="name: loud\ncapabilities: [prompt.inject]\n",
        body=f"""
        def register(ctx):
            ctx.register_system_prompt_section("rules", "x" * {plugins.MAX_PROMPT_SECTION_CHARS + 1})
        """,
    )
    caps.grant("loud", ["prompt.inject"])
    assert "per-section limit" in load_one(directory).error


def test_the_total_prompt_budget_is_enforced_across_sections(tmp_path):
    """One huge section, many small ones, and the total all have to be
    refused — the per-section limit alone leaves 32 * 4000 on the table."""
    chunk = plugins.MAX_PROMPT_SECTION_CHARS
    directory = write_plugin(
        tmp_path,
        "loud",
        manifest="name: loud\ncapabilities: [prompt.inject]\n",
        body=f"""
        def register(ctx):
            for index in range(5):
                ctx.register_system_prompt_section(f"s{{index}}", "x" * {chunk})
        """,
    )
    caps.grant("loud", ["prompt.inject"])
    entry = load_one(directory)
    assert "character limit" in entry.error
    total = sum(len(item.content) for item in plugins.manager().prompt_sections())
    assert total <= plugins.MAX_PROMPT_SECTIONS_TOTAL_CHARS


def test_no_sections_renders_nothing(tmp_path):
    assert plugins.render_prompt_sections() == ""


def test_prompt_sections_render_in_a_stable_order(tmp_path):
    """An unstable prefix is a cache miss on every request."""
    directory = write_plugin(
        tmp_path,
        "loud",
        manifest="name: loud\ncapabilities: [prompt.inject]\n",
        body="""
        def register(ctx):
            ctx.register_system_prompt_section("zeta", "Z")
            ctx.register_system_prompt_section("alpha", "A")
        """,
    )
    caps.grant("loud", ["prompt.inject"])
    assert load_one(directory).ok
    rendered = plugins.render_prompt_sections()
    assert rendered.index("loud:alpha") < rendered.index("loud:zeta")


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def test_state_round_trips(tmp_path):
    state = plugins.PluginState("thing")
    assert state.get("cursor") is None
    state.set("cursor", {"page": 2})
    assert state.get("cursor") == {"page": 2}
    assert state.keys() == ["cursor"]
    assert state.delete("cursor") is True
    assert state.delete("cursor") is False


@pytest.mark.parametrize("key", ["", "has space", "../escape", "a..b", "x" * 129])
def test_bad_state_keys_are_refused(key):
    with pytest.raises(ValueError):
        plugins.PluginState("thing").set(key, 1)


def test_the_state_quota_is_enforced(tmp_path):
    state = plugins.PluginState("thing")
    with pytest.raises(ValueError, match="quota exceeded"):
        state.set("big", "x" * (plugins.STATE_QUOTA_BYTES + 1))


def test_unserializable_state_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not JSON-serializable"):
        plugins.PluginState("thing").set("bad", object())


def test_state_is_written_private(tmp_path):
    state = plugins.PluginState("thing")
    state.set("k", "v")
    assert (state.path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# the event bus
# ---------------------------------------------------------------------------


def test_emit_reaches_a_subscriber(tmp_path, monkeypatch):
    write_plugin(
        tmp_path,
        "sender",
        body="""
        def register(ctx):
            ctx.register_command("fire", lambda raw: str(ctx.emit("ping", {"n": 1})))
        """,
    )
    write_plugin(
        tmp_path,
        "listener",
        body="""
        HEARD = []

        def register(ctx):
            ctx.subscribe("sender:ping", lambda payload: HEARD.append(payload))
        """,
    )
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    plugin_store.update("sender", enabled=True)
    plugin_store.update("listener", enabled=True)
    plugins.load()

    assert plugins.plugin_commands()["fire"].handler("") == "1"
    assert plugins.manager().loaded["listener"].module.HEARD == [{"n": 1}]


def test_a_plugin_cannot_emit_under_a_reserved_or_foreign_namespace(tmp_path):
    directory = write_plugin(
        tmp_path,
        "liar",
        body="""
        def register(ctx):
            ctx.emit("andromeda:session_start")
        """,
    )
    entry = load_one(directory)
    assert "may not emit" in entry.error
    assert "reserved for the host" in entry.error


def test_each_subscriber_gets_its_own_copy(tmp_path, monkeypatch):
    """One subscriber mutating the payload must not change what the next one
    sees, or the answer depends on registration order."""
    write_plugin(
        tmp_path,
        "sender",
        body="""
        def register(ctx):
            ctx.register_command("fire", lambda raw: ctx.emit("ping", {"items": []}))
        """,
    )
    write_plugin(
        tmp_path,
        "mutator",
        body="""
        def register(ctx):
            ctx.subscribe("sender:ping", lambda p: p["items"].append("mine"))
        """,
    )
    write_plugin(
        tmp_path,
        "reader",
        body="""
        SEEN = []

        def register(ctx):
            ctx.subscribe("sender:ping", lambda p: SEEN.append(list(p["items"])))
        """,
    )
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    for name in ("sender", "mutator", "reader"):
        plugin_store.update(name, enabled=True)
    plugins.load()

    plugins.plugin_commands()["fire"].handler("")
    assert plugins.manager().loaded["reader"].module.SEEN == [[]]


def test_a_raising_subscriber_does_not_stop_the_others(tmp_path, caplog):
    manager = plugins.PluginManager()

    def boom(payload):
        raise RuntimeError("nope")

    heard = []
    manager.subscribe_event("a", "a:ping", boom)
    manager.subscribe_event("b", "a:ping", heard.append)

    assert manager.dispatch_event("a:ping", {}) == 1
    assert heard == [{}]
    assert "nope" in caplog.text


def test_event_recursion_is_capped(caplog):
    """A plugin emitting from its own subscriber is the failure that actually
    happens, and it is the one an unbounded synchronous bus turns into a
    stack overflow."""
    manager = plugins.PluginManager()
    depths = []

    def again(payload):
        depths.append(len(depths))
        manager.dispatch_event("a:ping", {})

    manager.subscribe_event("a", "a:ping", again)
    manager.dispatch_event("a:ping", {})

    assert len(depths) == plugins.MAX_EVENT_DEPTH
    assert "levels deep" in caplog.text


def test_emitting_with_no_subscribers_is_zero():
    assert plugins.PluginManager().dispatch_event("a:ping", {}) == 0


# ---------------------------------------------------------------------------
# unload
# ---------------------------------------------------------------------------


def test_unload_runs_callbacks_in_reverse(tmp_path):
    directory = write_plugin(
        tmp_path,
        "closer",
        body="""
        CLOSED = []

        def register(ctx):
            ctx.on_unload(lambda: CLOSED.append("a"))
            ctx.on_unload(lambda: CLOSED.append("b"))
        """,
    )
    entry = load_one(directory)
    module = entry.module
    plugins.reset()
    assert module.CLOSED == ["b", "a"]


def test_unload_takes_hooks_off_the_bus(tmp_path):
    """A callback left registered fires from a module that has been removed
    from sys.modules — a reload then runs the version the user just replaced."""
    directory = write_plugin(
        tmp_path,
        "watcher",
        body="""
        SEEN = []

        def register(ctx):
            ctx.register_hook("post_tool_call", lambda **kw: SEEN.append(1))
        """,
    )
    entry = load_one(directory)
    module = entry.module
    hooks.invoke_hook("post_tool_call", tool_name="x")
    assert len(module.SEEN) == 1

    plugins.reset()
    hooks.invoke_hook("post_tool_call", tool_name="x")
    assert len(module.SEEN) == 1


def test_unload_does_not_disturb_other_hooks(tmp_path):
    """`hooks.reset()` would take the shell hooks with it."""
    kept = []
    hooks.register("post_tool_call", lambda **kw: kept.append(1))

    directory = write_plugin(
        tmp_path,
        "watcher",
        body="""
        def register(ctx):
            ctx.register_hook("post_tool_call", lambda **kw: None)
        """,
    )
    load_one(directory)
    plugins.reset()

    hooks.invoke_hook("post_tool_call", tool_name="x")
    assert kept == [1]


def test_a_raising_unload_callback_does_not_strand_the_rest(tmp_path, caplog):
    directory = write_plugin(
        tmp_path,
        "closer",
        body="""
        CLOSED = []

        def register(ctx):
            ctx.on_unload(lambda: CLOSED.append("a"))
            ctx.on_unload(_boom)

        def _boom():
            raise RuntimeError("nope")
        """,
    )
    module = load_one(directory).module
    plugins.reset()
    assert module.CLOSED == ["a"]
    assert "nope" in caplog.text


def test_unload_clears_every_registration(tmp_path):
    directory = write_plugin(
        tmp_path,
        "everything",
        body="""
        def register(ctx):
            ctx.register_tool("thing", "d", {}, lambda: "x")
            ctx.register_command("thing", lambda raw: None)
            ctx.register_cli_command("thing", "h", lambda p: None, lambda a: 0)
            ctx.register_redaction_patterns([r"zz-[0-9]{6}"])
        """,
    )
    assert load_one(directory).ok
    assert plugins.plugin_tool_specs()
    plugins.reset()
    assert plugins.plugin_tool_specs() == []
    assert plugins.plugin_commands() == {}
    assert plugins.plugin_cli_commands() == {}


def test_a_reload_re_executes_the_module(tmp_path):
    """A stale entry in sys.modules means an edit to a plugin does nothing
    until the interpreter restarts."""
    directory = write_plugin(
        tmp_path,
        "versioned",
        body="VERSION = 'one'\n\ndef register(ctx):\n    pass\n",
    )
    assert load_one(directory).module.VERSION == "one"
    plugins.reset()

    (directory / "__init__.py").write_text(
        "VERSION = 'two'\n\ndef register(ctx):\n    pass\n", encoding="utf-8"
    )
    assert load_one(directory).module.VERSION == "two"


# ---------------------------------------------------------------------------
# ctx helpers
# ---------------------------------------------------------------------------


def test_has_plugin_is_false_for_a_broken_dependency(tmp_path, monkeypatch):
    """A dependant should take its fallback path when its dependency is
    broken, not only when it is absent."""
    write_plugin(
        tmp_path,
        "adep",
        body="def register(ctx):\n    raise RuntimeError('broken')\n",
    )
    write_plugin(
        tmp_path,
        "bdep",
        manifest="name: bdep\nrequires_plugins: [adep]\n",
        body="""
        SAW = []

        def register(ctx):
            SAW.append(ctx.has_plugin("adep"))
        """,
    )
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    plugin_store.update("adep", enabled=True)
    plugin_store.update("bdep", enabled=True)
    plugins.load()

    assert plugins.manager().loaded["bdep"].module.SAW == [False]


def test_plugin_config_round_trips(tmp_path):
    directory = write_plugin(
        tmp_path,
        "settings",
        body="""
        SEEN = []

        def register(ctx):
            SEEN.append(ctx.get_config("page", "default"))
            ctx.set_config("page", 7)
            SEEN.append(ctx.get_config("page"))
        """,
    )
    entry = load_one(directory)
    assert entry.module.SEEN == ["default", 7]
    assert plugin_store.plugin_config("settings") == {"page": 7}


# ---------------------------------------------------------------------------
# drift guards
# ---------------------------------------------------------------------------


def test_builtin_tool_names_are_complete(tmp_path):
    """`BUILTIN_TOOL_NAMES` is written out rather than derived, so that a
    start does not have to build a registry. This is where that costs the
    suite instead of the user."""
    from andromeda_tools import build_registry
    from andromeda_tools.todo import TodoList
    from andromeda_tools.workspace import Workspace

    registry = build_registry(Workspace(str(tmp_path)), TodoList())
    missing = set(registry) - plugins.BUILTIN_TOOL_NAMES
    assert not missing, (
        f"these built-in tools are not in BUILTIN_TOOL_NAMES, so a plugin "
        f"could claim their names without asking: {sorted(missing)}"
    )


def test_builtin_command_names_match_the_repl():
    """A name missing here is a slash command a plugin can silently take."""
    import re

    source = (
        Path(__file__).resolve().parents[1] / "andromeda_cli" / "repl.py"
    ).read_text(encoding="utf-8")
    dispatched = set(re.findall(r'verb == "/([a-z]+)"', source))
    dispatched |= set(re.findall(r'verb in \{"/([a-z]+)", "/([a-z]+)"\}', source)[0])
    missing = dispatched - plugins.BUILTIN_COMMAND_NAMES
    assert not missing, f"not in BUILTIN_COMMAND_NAMES: {sorted(missing)}"
