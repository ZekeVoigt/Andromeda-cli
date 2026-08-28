"""Portable packages: `plugin.json`, skills and MCP servers, and no code.

The property every test here defends is the one the format exists for: nothing
in a portable package is ever executed by this process. Everything else — the
skills, the servers, the placeholders — follows from that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from andromeda_agent import hooks, plugin_store, plugins, portable


@pytest.fixture(autouse=True)
def clean_state():
    plugins.reset()
    hooks.reset()
    yield
    plugins.reset()
    hooks.reset()


def make_package(
    root: Path,
    *,
    name: str = "shipyard",
    manifest: dict | None = None,
    skills: dict[str, str] | None = None,
    mcp: dict | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    document = {"name": name, "version": "1.0.0", "description": "A package."}
    if manifest is not None:
        document = manifest
    (directory / "plugin.json").write_text(json.dumps(document), encoding="utf-8")

    for skill_name, content in (skills or {}).items():
        skill_dir = directory / "skills" / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    if mcp is not None:
        (directory / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    return directory


def skill_md(name: str, description: str = "Does a thing.", body: str = "Step one.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def load(directory: Path):
    manifest = plugins.read_portable_manifest(directory, "user")
    plugin_store.update(manifest.id, enabled=True)
    plugins.load({manifest.id: manifest})
    return plugins.manager().loaded[manifest.id]


# ---------------------------------------------------------------------------
# nothing runs
# ---------------------------------------------------------------------------


def test_a_portable_package_is_never_imported(tmp_path):
    """The whole point of the format. An `__init__.py` sitting beside a
    `plugin.json` is a file, not a plugin body."""
    directory = make_package(tmp_path, skills={"deploy": skill_md("deploy")})
    (directory / "__init__.py").write_text(
        "raise RuntimeError('this must never run')\n", encoding="utf-8"
    )
    entry = load(directory)
    assert entry.ok, entry.error
    assert entry.module is None


def test_a_portable_package_cannot_declare_a_capability(tmp_path):
    """Refused rather than ignored: an author whose field sits there unread
    goes on believing it works."""
    directory = make_package(
        tmp_path,
        manifest={"name": "greedy", "version": "1.0.0", "capabilities": ["tools.override"]},
    )
    with pytest.raises(portable.PortableError, match="declares no capabilities"):
        portable.load(directory)


def test_the_manifest_flag_says_which_kind_it_is(tmp_path):
    directory = make_package(tmp_path)
    assert plugins.read_portable_manifest(directory, "user").portable is True


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Shipyard", "ship yard", "-ship", "ship-", "ship--yard", "ship..yard", ""]
)
def test_an_unusable_name_is_fatal(tmp_path, name):
    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1"}), encoding="utf-8"
    )
    with pytest.raises(portable.PortableError, match="`name` must be"):
        portable.load(directory)


def test_malformed_json_is_fatal(tmp_path):
    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / "plugin.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(portable.PortableError, match="not valid JSON"):
        portable.load(directory)


def test_a_missing_manifest_is_fatal(tmp_path):
    with pytest.raises(portable.PortableError, match="does not exist"):
        portable.load(tmp_path / "absent")


def test_an_unknown_schema_is_a_note_not_a_refusal(tmp_path):
    """A package targeting a newer schema still installs; the listing names
    what was not read."""
    directory = make_package(
        tmp_path,
        manifest={
            "$schema": "https://agent-plugins.org/schemas/9.0.0/plugin.schema.json",
            "name": "future",
            "version": "1.0.0",
        },
    )
    package = portable.load(directory)
    assert any("9.0.0" in note.message for note in package.notes)


def test_unknown_fields_are_reported(tmp_path):
    directory = make_package(
        tmp_path,
        manifest={"name": "pkg", "version": "1.0.0", "capabilties": []},
    )
    assert portable.load(directory).unknown_fields == ("capabilties",)


def test_an_author_table_is_flattened(tmp_path):
    directory = make_package(
        tmp_path,
        manifest={"name": "pkg", "version": "1", "author": {"name": "Ada", "email": "a@b.c"}},
    )
    assert portable.load(directory).author == "Ada, a@b.c"


def test_an_overlong_description_is_truncated(tmp_path):
    directory = make_package(
        tmp_path,
        manifest={"name": "pkg", "version": "1", "description": "x" * 5000},
    )
    package = portable.load(directory)
    assert len(package.description) == portable.MAX_DESCRIPTION
    assert any("truncated" in note.message for note in package.notes)


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------


def test_a_skill_is_namespaced_and_loadable(tmp_path):
    from andromeda_tools import skills as skills_module

    directory = make_package(
        tmp_path, skills={"deploy": skill_md("deploy", body="Migrate first.")}
    )
    assert load(directory).ok
    assert "shipyard:deploy" in plugins.plugin_skills()
    assert "Migrate first." in skills_module.load_skill({}, "shipyard:deploy").content


def test_a_skill_whose_name_does_not_match_its_directory_is_a_note(tmp_path):
    """The directory is how the skill is reached, so a manifest naming
    something else names a skill nobody can load."""
    directory = make_package(tmp_path, skills={"deploy": skill_md("release")})
    package = portable.load(directory)
    assert package.skills == []
    assert any("does not match the directory" in note.message for note in package.notes)


def test_a_skill_with_no_description_is_a_note(tmp_path):
    directory = make_package(
        tmp_path, skills={"deploy": "---\nname: deploy\n---\nbody"}
    )
    package = portable.load(directory)
    assert package.skills == []
    assert any("non-empty `description`" in note.message for note in package.notes)


def test_a_skill_without_frontmatter_is_a_note(tmp_path):
    directory = make_package(tmp_path, skills={"deploy": "just a body"})
    package = portable.load(directory)
    assert any("no YAML frontmatter" in note.message for note in package.notes)


def test_unterminated_frontmatter_is_a_note(tmp_path):
    directory = make_package(tmp_path, skills={"deploy": "---\nname: deploy\nbody"})
    package = portable.load(directory)
    assert any("unterminated" in note.message for note in package.notes)


def test_one_broken_skill_does_not_cost_the_others(tmp_path):
    """The parts are independent, and the author can see which one failed."""
    directory = make_package(
        tmp_path,
        skills={"good": skill_md("good"), "bad": "no frontmatter here"},
    )
    package = portable.load(directory)
    assert [skill.name for skill in package.skills] == ["good"]
    assert package.notes


def test_a_skill_directory_with_no_skill_md_is_skipped_quietly(tmp_path):
    directory = make_package(tmp_path, skills={"deploy": skill_md("deploy")})
    (directory / "skills" / "notes").mkdir()
    package = portable.load(directory)
    assert [skill.name for skill in package.skills] == ["deploy"]
    assert not package.notes


def test_no_skills_directory_is_fine(tmp_path):
    assert portable.load(make_package(tmp_path)).skills == []


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


def test_a_server_is_namespaced_by_the_package(tmp_path):
    """Two packages carrying a server called `github` must not become one, and
    a package server must never shadow one the user configured."""
    directory = make_package(
        tmp_path, mcp={"mcpServers": {"files": {"command": "npx", "args": []}}}
    )
    assert list(portable.load(directory).mcp_servers) == ["shipyard:files"]


def test_placeholders_expand_everywhere(tmp_path):
    directory = make_package(
        tmp_path,
        mcp={
            "mcpServers": {
                "files": {
                    "command": "${PLUGIN_ROOT}/bin/serve",
                    "args": ["--root", "${PLUGIN_ROOT}"],
                    "env": {"CACHE": "${PLUGIN_DATA}/cache"},
                }
            }
        },
    )
    config = portable.load(directory).mcp_servers["shipyard:files"]
    assert str(directory) in config["command"]
    assert str(directory) in config["args"][1]
    assert "plugin-data/shipyard/cache" in config["env"]["CACHE"]


def test_an_unknown_placeholder_is_left_alone(tmp_path):
    """Only two exist, and both resolve to directories this harness owns. A
    package cannot invent a third and have it substituted."""
    directory = make_package(
        tmp_path,
        mcp={"mcpServers": {"x": {"command": "${HOME}/bin/serve"}}},
    )
    assert portable.load(directory).mcp_servers["shipyard:x"]["command"] == "${HOME}/bin/serve"


def test_a_cwd_outside_the_package_is_dropped(tmp_path):
    directory = make_package(
        tmp_path, mcp={"mcpServers": {"x": {"command": "serve", "cwd": "/etc"}}}
    )
    package = portable.load(directory)
    assert "cwd" not in package.mcp_servers["shipyard:x"]
    assert any("resolves outside" in note.message for note in package.notes)


def test_a_cwd_inside_the_package_survives(tmp_path):
    directory = make_package(
        tmp_path, mcp={"mcpServers": {"x": {"command": "serve", "cwd": "${PLUGIN_ROOT}"}}}
    )
    assert portable.load(directory).mcp_servers["shipyard:x"]["cwd"] == str(directory)


def test_a_malformed_mcp_file_is_a_note(tmp_path):
    directory = make_package(tmp_path)
    (directory / "mcp.json").write_text("{broken", encoding="utf-8")
    package = portable.load(directory)
    assert package.mcp_servers == {}
    assert any("cannot read mcp.json" in note.message for note in package.notes)


def test_a_file_without_mcpservers_is_a_note(tmp_path):
    directory = make_package(tmp_path, mcp={"servers": {}})
    assert any("no `mcpServers`" in note.message for note in portable.load(directory).notes)


def test_a_package_server_reaches_build_servers(tmp_path):
    from andromeda_tools import mcp as mcp_module

    directory = make_package(
        tmp_path, mcp={"mcpServers": {"files": {"command": "npx", "args": []}}}
    )
    assert load(directory).ok
    names = [server.name for server in mcp_module.build_servers(tmp_path)]
    assert "shipyard:files" in names


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def test_an_empty_package_says_so(tmp_path):
    """"I installed it and nothing happened" is otherwise unanswerable."""
    entry = load(make_package(tmp_path))
    assert entry.ok
    assert any("carries no skills" in note for note in entry.notes)


def test_load_notes_survive_onto_the_entry(tmp_path):
    entry = load(make_package(tmp_path, skills={"deploy": "no frontmatter"}))
    assert any("no YAML frontmatter" in note for note in entry.notes)


def test_unloading_clears_a_portable_packages_registrations(tmp_path):
    directory = make_package(
        tmp_path,
        skills={"deploy": skill_md("deploy")},
        mcp={"mcpServers": {"files": {"command": "npx"}}},
    )
    assert load(directory).ok
    assert plugins.plugin_skills() and plugins.mcp_servers()

    plugins.reset()
    assert plugins.plugin_skills() == {}
    assert plugins.mcp_servers() == {}


def test_discovery_finds_both_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)

    make_package(tmp_path, name="portable-one", skills={"a": skill_md("a")})
    python_one = tmp_path / "python-one"
    python_one.mkdir()
    (python_one / "plugin.yaml").write_text("name: python-one\n", encoding="utf-8")
    (python_one / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")

    found = plugins.discover()
    assert set(found) == {"portable-one", "python-one"}
    assert found["portable-one"].portable is True
    assert found["python-one"].portable is False


def test_a_broken_portable_package_skips_only_itself(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)

    make_package(tmp_path, name="good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "plugin.json").write_text("{not json", encoding="utf-8")

    assert set(plugins.discover()) == {"good"}
    assert "bad" in caplog.text


def test_is_portable_only_matches_a_real_manifest(tmp_path):
    assert portable.is_portable(tmp_path) is False
    (tmp_path / "plugin.json").write_text("{}", encoding="utf-8")
    assert portable.is_portable(tmp_path) is True
