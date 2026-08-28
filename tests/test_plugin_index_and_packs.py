"""The community index and the pack format.

Both exist to make a plugin easier to reach. Every test here is about the
places that convenience must not reach into: an index entry that could point
somewhere different tomorrow, a pack that could grant a capability, a pack
that could carry a credential.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from andromeda_agent import hooks, plugin_index, plugin_packs, plugin_store, plugins

SHA = "4f1c2b9a8e7d6c5b4a3928170615243342516070"
OTHER_SHA = "8f3c2d1a9b4e5f6071829304a5b6c7d8e9f00112"


@pytest.fixture(autouse=True)
def clean_state():
    plugins.reset()
    hooks.reset()
    yield
    plugins.reset()
    hooks.reset()


def write_cache(entries: list[dict]) -> Path:
    path = plugin_index.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": entries}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------


def test_a_moving_ref_is_dropped(caplog):
    """A tag can be moved and a branch head moves by definition, so an index
    that resolved to either would resolve to different code tomorrow with the
    same words in it."""
    entries = plugin_index.parse(
        {
            "plugins": [
                {"name": "good", "repo": "o/g", "ref": SHA},
                {"name": "loose", "repo": "o/l", "ref": "main"},
                {"name": "tagged", "repo": "o/t", "ref": "v1.0.0"},
            ]
        }
    )
    assert [entry.name for entry in entries] == ["good"]
    assert "not a 40-character commit SHA" in caplog.text


def test_a_duplicate_name_keeps_the_first(caplog):
    """A duplicate in a community index is either a mistake or a typosquat,
    and picking the later one silently is the worse answer to both."""
    entries = plugin_index.parse(
        {
            "plugins": [
                {"name": "thing", "repo": "real/thing", "ref": SHA},
                {"name": "thing", "repo": "evil/thing", "ref": OTHER_SHA},
            ]
        }
    )
    assert len(entries) == 1
    assert entries[0].repo == "real/thing"
    assert "more than once" in caplog.text


@pytest.mark.parametrize(
    "row",
    [
        {"repo": "o/r", "ref": SHA},
        {"name": "no-repo", "ref": SHA},
        {"name": "Bad Name", "repo": "o/r", "ref": SHA},
        {"name": "../escape", "repo": "o/r", "ref": SHA},
    ],
)
def test_an_unusable_entry_is_dropped_not_fatal(row):
    """One malformed row must not take the whole index away from everybody."""
    assert plugin_index.parse({"plugins": [row, {"name": "ok", "repo": "o/o", "ref": SHA}]}) == [
        plugin_index.IndexEntry(name="ok", repo="o/o", ref=SHA)
    ]


def test_a_bare_list_is_accepted():
    assert len(plugin_index.parse([{"name": "a", "repo": "o/a", "ref": SHA}])) == 1


def test_nonsense_parses_to_nothing():
    assert plugin_index.parse("not an index") == []
    assert plugin_index.parse({"plugins": "nope"}) == []


def test_a_fresh_cache_is_used_without_the_network(monkeypatch):
    write_cache([{"name": "cached", "repo": "o/c", "ref": SHA}])
    monkeypatch.setattr(
        plugin_index, "_download", lambda: pytest.fail("should not have fetched")
    )
    entries, source = plugin_index.fetch()
    assert source == "cache"
    assert entries[0].name == "cached"


def test_a_stale_cache_beats_the_seed(monkeypatch):
    """It is at least this install's own view of the index at some point,
    while the seed is whatever was true at release."""
    path = write_cache([{"name": "stale", "repo": "o/s", "ref": SHA}])
    old = time.time() - (plugin_index.CACHE_TTL_SECONDS + 60)
    import os

    os.utime(path, (old, old))
    monkeypatch.setattr(plugin_index, "_download", lambda: None)

    entries, source = plugin_index.fetch()
    assert source == "stale cache"
    assert entries[0].name == "stale"


def test_no_cache_and_no_network_falls_back_to_the_seed(monkeypatch):
    monkeypatch.setattr(plugin_index, "_download", lambda: None)
    _entries, source = plugin_index.fetch()
    assert source == "bundled"


def test_the_bundled_seed_is_valid_and_shipped():
    """It is the offline fallback *and* the format reference, so a seed that
    does not parse is a format reference that teaches the wrong thing."""
    assert plugin_index.seed_path().exists()
    payload = json.loads(plugin_index.seed_path().read_text(encoding="utf-8"))
    assert isinstance(plugin_index.parse(payload), list)


def test_a_successful_fetch_is_cached(monkeypatch):
    monkeypatch.setattr(
        plugin_index,
        "_download",
        lambda: {"plugins": [{"name": "fresh", "repo": "o/f", "ref": SHA}]},
    )
    entries, source = plugin_index.fetch(force=True)
    assert source == "network"
    assert entries[0].name == "fresh"
    assert plugin_index.cache_path().exists()


def test_search_prefers_a_name_match(monkeypatch):
    monkeypatch.setattr(
        plugin_index,
        "_download",
        lambda: {
            "plugins": [
                {"name": "aaa", "repo": "o/a", "ref": SHA, "description": "about tides"},
                {"name": "tides", "repo": "o/t", "ref": OTHER_SHA},
            ]
        },
    )
    matched, _source = plugin_index.search("tides")
    assert [entry.name for entry in matched] == ["tides", "aaa"]


def test_resolve_is_exact(monkeypatch):
    monkeypatch.setattr(
        plugin_index,
        "_download",
        lambda: {"plugins": [{"name": "tides", "repo": "o/t", "ref": SHA}]},
    )
    assert plugin_index.resolve("tides").repo == "o/t"
    assert plugin_index.resolve("tide") is None


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("weather", True),
        ("owner/repo", False),
        ("https://example.test/x.git", False),
        ("./local", False),
        ("~/local", False),
        ("--flag", False),
        ("", False),
    ],
)
def test_only_a_bare_name_is_looked_up(identifier, expected):
    """Anything with a slash or a scheme is a location the user gave us, and
    treating it as a name would turn a typo in a path into a remote lookup."""
    assert plugin_index.looks_like_bare_name(identifier) is expected


# ---------------------------------------------------------------------------
# packs
# ---------------------------------------------------------------------------


def pack_file(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_a_valid_pack_parses(tmp_path):
    pack = plugin_packs.load(
        pack_file(
            tmp_path,
            {
                "name": "desk",
                "description": "Mine.",
                "plugins": [
                    {"name": "wordcount", "ref": SHA},
                    {"repo": "owner/thesaurus", "ref": OTHER_SHA, "subdir": "plugin"},
                ],
                "config": {"wordcount": {"target": 800}},
            },
        )
    )
    assert pack.name == "desk"
    assert [entry.source for entry in pack.entries] == ["wordcount", "owner/thesaurus"]
    assert pack.config == {"wordcount": {"target": 800}}


def test_a_moving_ref_is_refused_by_name(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="thing pins 'main'"):
        plugin_packs.load(
            pack_file(tmp_path, {"name": "p", "plugins": [{"name": "thing", "ref": "main"}]})
        )


def test_an_entry_naming_both_a_name_and_a_repo_is_refused(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="names both"):
        plugin_packs.load(
            pack_file(
                tmp_path,
                {"name": "p", "plugins": [{"name": "a", "repo": "o/a", "ref": SHA}]},
            )
        )


def test_an_entry_naming_neither_is_refused(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="names neither"):
        plugin_packs.load(pack_file(tmp_path, {"name": "p", "plugins": [{"ref": SHA}]}))


def test_a_duplicate_entry_is_refused(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="listed twice"):
        plugin_packs.load(
            pack_file(
                tmp_path,
                {
                    "name": "p",
                    "plugins": [{"name": "a", "ref": SHA}, {"name": "a", "ref": OTHER_SHA}],
                },
            )
        )


def test_a_pack_with_no_plugins_is_refused(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="lists no plugins"):
        plugin_packs.load(pack_file(tmp_path, {"name": "p", "plugins": []}))


def test_a_pack_with_no_name_is_refused(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="no `name:`"):
        plugin_packs.load(pack_file(tmp_path, {"plugins": [{"name": "a", "ref": SHA}]}))


@pytest.mark.parametrize(
    "key",
    ["api_key", "apiKey", "OPENAI_API_KEY", "auth_token", "password", "client_secret"],
)
def test_a_credential_shaped_setting_is_refused(tmp_path, key):
    """A pack is a file people share, so it must not be the convenient place
    to put one."""
    with pytest.raises(plugin_packs.PackError, match="looks like a credential"):
        plugin_packs.load(
            pack_file(
                tmp_path,
                {
                    "name": "p",
                    "plugins": [{"name": "a", "ref": SHA}],
                    "config": {"a": {key: "value"}},
                },
            )
        )


@pytest.mark.parametrize(
    "key", ["granted_capabilities", "capability_hash", "enabled", "granted_at"]
)
def test_a_pack_cannot_write_a_trust_decision(tmp_path, key):
    """The rule the format exists to protect. A file that could pre-approve
    `tools.override` would make consent a formality arriving after the
    decision."""
    with pytest.raises(plugin_packs.PackError, match="trust decision"):
        plugin_packs.load(
            pack_file(
                tmp_path,
                {
                    "name": "p",
                    "plugins": [{"name": "a", "ref": SHA}],
                    "config": {"a": {key: True}},
                },
            )
        )


def test_ordinary_settings_survive(tmp_path):
    pack = plugin_packs.load(
        pack_file(
            tmp_path,
            {
                "name": "p",
                "plugins": [{"name": "a", "ref": SHA}],
                "config": {"a": {"target": 800, "style": "terse"}},
            },
        )
    )
    assert pack.config == {"a": {"target": 800, "style": "terse"}}


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(plugin_packs.PackError, match="does not exist"):
        plugin_packs.load(tmp_path / "absent.yaml")


def test_malformed_yaml_says_so(tmp_path):
    path = tmp_path / "pack.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(plugin_packs.PackError, match="not valid YAML"):
        plugin_packs.load(path)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def make_installed(tmp_path: Path, plugin_id: str, *, ref: str, origin: str) -> None:
    directory = tmp_path / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(f"name: {plugin_id}\n", encoding="utf-8")
    (directory / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    plugin_store.update(plugin_id, enabled=True, ref=ref, origin=origin, source="user")


def test_export_names_what_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    make_installed(tmp_path, "alpha", ref=SHA, origin="owner/alpha")
    plugin_store.set_plugin_config("alpha", "target", 800)

    document = plugin_packs.export("mine")
    assert document["plugins"] == [{"ref": SHA, "repo": "owner/alpha"}]
    assert document["config"] == {"alpha": {"target": 800}}


def test_export_skips_what_cannot_be_pinned(tmp_path, monkeypatch):
    """A pack naming a placeholder would fail validation on the machine it was
    shared with, which is a worse outcome than an honest omission."""
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    make_installed(tmp_path, "local", ref="", origin="/home/me/local")

    document = plugin_packs.export("mine")
    assert document["plugins"] == []
    assert "local" in document["$skipped"]


def test_export_omits_a_disabled_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    make_installed(tmp_path, "alpha", ref=SHA, origin="owner/alpha")
    plugin_store.update("alpha", enabled=False)

    assert plugin_packs.export("mine")["plugins"] == []


def test_export_never_writes_a_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    make_installed(tmp_path, "alpha", ref=SHA, origin="owner/alpha")
    plugin_store.set_plugin_config("alpha", "api_key", "sk-secret")
    plugin_store.set_plugin_config("alpha", "target", 800)

    document = plugin_packs.export("mine")
    assert document["config"] == {"alpha": {"target": 800}}
    assert "sk-secret" not in plugin_packs.to_yaml(document)


def test_an_exported_pack_reimports(tmp_path, monkeypatch):
    """The round trip is the point: an export that its own parser refuses is
    an export nobody can use."""
    monkeypatch.setattr(plugins, "bundled_dir", lambda: None)
    monkeypatch.setattr(plugins, "user_dir", lambda: tmp_path)
    make_installed(tmp_path, "alpha", ref=SHA, origin="owner/alpha")

    rendered = plugin_packs.to_yaml(plugin_packs.export("mine", "round trip"))
    path = tmp_path / "out.yaml"
    path.write_text(rendered, encoding="utf-8")

    pack = plugin_packs.load(path)
    assert pack.name == "mine"
    assert pack.entries[0].ref == SHA
