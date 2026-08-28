"""Capabilities: the registry, the grant record, and the enforcement."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from andromeda_agent import plugin_capabilities as caps
from andromeda_agent import plugin_store

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_every_capability_has_an_enforcing_gate():
    """The rule the module opens with. An id that reads like a permission and
    gates nothing teaches people that consent screens are noise, so this fails
    the moment one is minted without a `_require` call behind it."""
    source = (PACKAGE_ROOT / "andromeda_agent" / "plugins.py").read_text(
        encoding="utf-8"
    )
    enforced = set(re.findall(r'self\._require\("([a-z.]+)"\)', source))
    unenforced = caps.VALID_IDS - enforced
    assert not unenforced, (
        f"these capabilities are declared but nothing checks them: "
        f"{sorted(unenforced)}"
    )


def test_no_capability_is_checked_without_being_declared():
    """The other direction: a `_require` for an id that is not in the registry
    would raise ValueError at the moment a plugin reached that seam."""
    source = (PACKAGE_ROOT / "andromeda_agent" / "plugins.py").read_text(
        encoding="utf-8"
    )
    enforced = set(re.findall(r'self\._require\("([a-z.]+)"\)', source))
    assert not (enforced - caps.VALID_IDS)


def test_each_spec_names_the_method_it_gates():
    for spec in caps.CAPABILITIES:
        assert spec.gate.startswith("ctx.register")
        assert spec.description


# ---------------------------------------------------------------------------
# declaring
# ---------------------------------------------------------------------------


def test_declared_capabilities_split_into_known_and_unknown():
    known, unknown = caps.parse_declared(["tools.override", "warp.drive"])
    assert known == ["tools.override"]
    assert unknown == ["warp.drive"]


def test_a_bare_string_is_accepted():
    assert caps.parse_declared("tools.override") == (["tools.override"], [])


@pytest.mark.parametrize("raw", [None, 42, {"a": 1}])
def test_a_nonsense_declaration_is_empty(raw):
    assert caps.parse_declared(raw) == ([], [])


def test_duplicates_are_collapsed():
    known, _ = caps.parse_declared(["tools.override", "tools.override"])
    assert known == ["tools.override"]


# ---------------------------------------------------------------------------
# granting
# ---------------------------------------------------------------------------


def test_a_grant_round_trips():
    caps.grant("thing", ["tools.override"])
    assert caps.granted("thing") == {"tools.override"}
    assert caps.is_granted("thing", "tools.override")


def test_a_grant_replaces_rather_than_accumulates():
    """A grant is a snapshot of one decision. Merging them across updates is
    how a plugin ends up holding a capability nobody remembers approving."""
    caps.grant("thing", ["tools.override", "prompt.inject"])
    caps.grant("thing", ["prompt.inject"])
    assert caps.granted("thing") == {"prompt.inject"}


def test_an_unknown_id_is_never_granted():
    caps.grant("thing", ["tools.override", "warp.drive"])
    assert caps.granted("thing") == {"tools.override"}


def test_a_hand_edited_grant_is_discarded_whole():
    """A list that does not match its own hash means the ledger was edited by
    hand, and a half-trusted grant record is not something to reason about."""
    caps.grant("thing", ["prompt.inject"])
    plugin_store.update(
        "thing", **{caps.GRANTED_KEY: ["prompt.inject", "tools.override"]}
    )
    assert caps.granted("thing") == frozenset()


def test_reordering_a_manifest_does_not_re_prompt():
    caps.grant("thing", ["tools.override", "prompt.inject"])
    assert caps.needs_consent("thing", ["prompt.inject", "tools.override"]) == []


def test_an_added_capability_needs_consent():
    caps.grant("thing", ["prompt.inject"])
    assert caps.needs_consent("thing", ["prompt.inject", "tools.override"]) == [
        "tools.override"
    ]


def test_dropping_a_capability_needs_no_consent():
    """Giving up authority is never a thing to prompt about."""
    caps.grant("thing", ["prompt.inject", "tools.override"])
    assert caps.needs_consent("thing", ["prompt.inject"]) == []


def test_revoking_leaves_nothing():
    caps.grant("thing", ["tools.override"])
    caps.revoke("thing")
    assert caps.granted("thing") == frozenset()


def test_the_grant_records_when():
    caps.grant("thing", ["tools.override"])
    assert plugin_store.entry("thing")[caps.GRANTED_AT_KEY].endswith("Z")


# ---------------------------------------------------------------------------
# requiring
# ---------------------------------------------------------------------------


def test_require_passes_when_granted():
    caps.grant("thing", ["tools.override"])
    caps.require("thing", "Thing", "tools.override")


def test_require_names_the_fix():
    """The person reading the message is usually not the person who wrote the
    plugin, so it has to name the command that resolves it."""
    with pytest.raises(caps.CapabilityError) as raised:
        caps.require("thing", "Thing", "tools.override")
    message = str(raised.value)
    assert "andromeda plugins enable thing" in message
    assert "Replace a built-in tool" in message


def test_require_refuses_an_unknown_capability():
    with pytest.raises(ValueError):
        caps.require("thing", "Thing", "warp.drive")


def test_describe_falls_back_to_the_id():
    assert caps.describe("warp.drive") == "warp.drive"
    assert "built-in tool" in caps.describe("tools.override")


# ---------------------------------------------------------------------------
# the ledger underneath
# ---------------------------------------------------------------------------


def test_an_unreadable_ledger_reads_as_nothing_granted(caplog):
    """The only reading that cannot turn a broken file into an accidental
    grant."""
    plugin_store.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    plugin_store.ledger_path().write_text("{not json", encoding="utf-8")

    assert plugin_store.load() == {"entries": {}}
    assert plugin_store.is_enabled("anything") is False
    assert caps.granted("anything") == frozenset()
    assert "plugin ledger" in caplog.text


def test_a_non_mapping_entry_is_dropped():
    plugin_store.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    plugin_store.ledger_path().write_text(
        '{"entries": {"a": ["not", "a", "row"], "b": {"enabled": true}}}',
        encoding="utf-8",
    )
    assert set(plugin_store.load()["entries"]) == {"b"}


def test_enabled_is_never_implied():
    """The default is False and stays False — an installed-but-never-enabled
    plugin is Python that has not been imported."""
    plugin_store.update("thing", source="user")
    assert plugin_store.is_enabled("thing") is False
    plugin_store.update("thing", enabled="yes")
    assert plugin_store.is_enabled("thing") is False
    plugin_store.update("thing", enabled=True)
    assert plugin_store.is_enabled("thing") is True


def test_removing_a_row_forgets_the_grant():
    caps.grant("thing", ["tools.override"])
    assert plugin_store.remove("thing") is True
    assert caps.granted("thing") == frozenset()
    assert plugin_store.remove("thing") is False


def test_the_ledger_is_written_private():
    plugin_store.update("thing", enabled=True)
    assert (plugin_store.ledger_path().stat().st_mode & 0o777) == 0o600
