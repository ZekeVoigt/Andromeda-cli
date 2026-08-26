"""The skill library keeping itself honest: usage, the sweep, the review."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from andromeda_agent import curator
from andromeda_tools import skill_usage
from andromeda_tools import skills as skills_module


def write_skill(root: Path, name: str, body: str = "Steps.", by_agent: bool = True) -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    provenance = (
        "metadata:\n  andromeda:\n    created_by: agent\n" if by_agent else ""
    )
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: does {name}\n{provenance}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def discovered(home: Path) -> dict[str, skills_module.Skill]:
    return {
        name: skill
        for name, skill in skills_module.discover(home).items()
        if str(home) in str(skill.path)
    }


def age(home: Path, name: str, days: float, uses: int = 1) -> None:
    """Backdate a record, which is the only way to test a clock."""
    when = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    data = skill_usage.load(home)
    record = data.get(name) or skill_usage.blank_record()
    record["created_at"] = when
    record["last_used_at"] = when if uses else ""
    record["uses"] = uses
    data[name] = record
    skill_usage.save(home, data)


@pytest.fixture
def home(tmp_path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    return root


SETTINGS = curator.Settings(
    enabled=True, interval_days=7, stale_after_days=30, archive_after_days=90
)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def test_a_skill_the_agent_wrote_is_curatable(home):
    write_skill(home, "made-up")
    skill = discovered(home)["made-up"]
    assert skill_usage.is_agent_created(skill) is True
    assert skill_usage.is_curatable(skill, home) is True


def test_a_skill_you_wrote_is_left_alone(home):
    """Provenance is a marker in the file, not a guess from a path."""
    write_skill(home, "mine", by_agent=False)
    skill = discovered(home)["mine"]
    assert skill_usage.is_agent_created(skill) is False
    assert skill_usage.is_curatable(skill, home) is False


def test_an_agent_skill_inside_a_workspace_is_left_alone(tmp_path, home):
    """It belongs to that repository. Moving it would be this program editing
    somebody else's project."""
    workspace = tmp_path / "repo"
    write_skill(workspace, "theirs")
    skill = skills_module.discover(workspace)["theirs"]
    assert skill_usage.is_agent_created(skill) is True
    assert skill_usage.is_curatable(skill, home) is False


def test_a_skill_can_be_marked_as_the_agents(home):
    directory = write_skill(home, "unmarked", by_agent=False)
    path = directory / "SKILL.md"

    assert skill_usage.mark_created_by_agent(path) is True
    assert "created_by: agent" in path.read_text()
    # Idempotent.
    assert skill_usage.mark_created_by_agent(path) is False


def test_marking_leaves_an_existing_metadata_block_alone(home):
    """Editing somebody's YAML by string surgery is how a skill stops
    parsing. Not marking one costs curation, which is the safe direction."""
    directory = home / "skills" / "complex"
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        "---\nname: complex\nmetadata:\n  andromeda:\n    requires:\n      bins: [git]\n---\nbody\n",
        encoding="utf-8",
    )

    assert skill_usage.mark_created_by_agent(path) is False
    assert "requires" in path.read_text()


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


def test_loading_a_skill_records_it(home):
    write_skill(home, "made-up")
    skills = discovered(home)

    skills_module.load_skill(skills, "made-up", home=home)

    record = skill_usage.record_for(home, "made-up")
    assert record["uses"] == 1
    assert record["last_used_at"]


def test_loading_twice_counts_twice(home):
    write_skill(home, "made-up")
    skills = discovered(home)
    skills_module.load_skill(skills, "made-up", home=home)
    skills_module.load_skill(skills, "made-up", home=home)
    assert skill_usage.record_for(home, "made-up")["uses"] == 2


def test_using_a_stale_skill_brings_it_back(home):
    """Waiting for the next sweep would leave the library describing something
    that is no longer true."""
    write_skill(home, "made-up")
    skill_usage.seed(home, "made-up")
    skill_usage.set_state(home, "made-up", skill_usage.STALE)

    skills_module.load_skill(discovered(home), "made-up", home=home)

    assert skill_usage.record_for(home, "made-up")["state"] == skill_usage.ACTIVE


def test_a_failed_record_never_fails_the_load(home, monkeypatch):
    write_skill(home, "made-up")
    monkeypatch.setattr(
        skill_usage, "save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    )
    result = skills_module.load_skill(discovered(home), "made-up", home=home)
    assert result.ok is True


def test_a_corrupt_usage_file_reads_as_empty(home):
    skill_usage.usage_path(home).write_text("{not json", encoding="utf-8")
    assert skill_usage.load(home) == {}


def test_the_report_says_how_idle_a_skill_is(home):
    write_skill(home, "made-up")
    age(home, "made-up", days=45)

    row = skill_usage.report(discovered(home), home)[0]

    assert row["name"] == "made-up"
    assert 44 < row["idle_days"] < 46
    assert row["recorded"] is True


def test_the_report_can_include_what_it_does_not_curate(home):
    write_skill(home, "mine", by_agent=False)
    assert skill_usage.report(discovered(home), home) == []
    assert skill_usage.report(discovered(home), home, include_uncurated=True)


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def test_the_first_sight_of_a_skill_only_starts_its_clock(home):
    """A skill that predates the record is not evidence of neglect."""
    write_skill(home, "made-up")

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.seeded == 1
    assert result.archived == 0
    assert skill_usage.record_for(home, "made-up")["state"] == skill_usage.ACTIVE


def test_an_idle_skill_goes_stale(home):
    write_skill(home, "made-up")
    age(home, "made-up", days=45)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.marked_stale == 1
    assert skill_usage.record_for(home, "made-up")["state"] == skill_usage.STALE


def test_a_long_idle_skill_is_archived(home):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=200)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.archived == 1
    assert not directory.exists()
    assert "made-up" in skill_usage.archived_names(home)


def test_an_archived_skill_can_be_restored(home):
    write_skill(home, "made-up")
    age(home, "made-up", days=200)
    curator.sweep(discovered(home), home, SETTINGS)

    ok, where = skill_usage.restore(home, "made-up")

    assert ok is True
    assert (home / "skills" / "made-up" / "SKILL.md").exists()
    assert skill_usage.record_for(home, "made-up")["state"] == skill_usage.ACTIVE


def test_nothing_is_ever_deleted(home):
    """The worst outcome of a wrong decision is a restore."""
    write_skill(home, "made-up", body="something worth keeping")
    age(home, "made-up", days=200)

    curator.sweep(discovered(home), home, SETTINGS)

    archived = skill_usage.archive_dir(home) / "made-up" / "SKILL.md"
    assert "something worth keeping" in archived.read_text()


def test_a_pinned_skill_is_never_touched(home):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=500)
    skill_usage.set_pinned(home, "made-up", True)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.skipped_pinned == 1
    assert result.archived == 0
    assert directory.exists()


def test_a_never_used_skill_gets_a_grace_period(home):
    """Absence of evidence. Its trigger may simply not have come up yet."""
    write_skill(home, "made-up")
    age(home, "made-up", days=10, uses=0)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.marked_stale == 0
    assert skill_usage.record_for(home, "made-up")["state"] == skill_usage.ACTIVE


def test_a_never_used_skill_does_eventually_go_stale(home):
    write_skill(home, "made-up")
    age(home, "made-up", days=45, uses=0)
    result = curator.sweep(discovered(home), home, SETTINGS)
    assert result.marked_stale == 1


def test_a_used_skill_comes_back_from_stale(home):
    write_skill(home, "made-up")
    age(home, "made-up", days=1)
    skill_usage.set_state(home, "made-up", skill_usage.STALE)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.revived == 1
    assert skill_usage.record_for(home, "made-up")["state"] == skill_usage.ACTIVE


def test_a_dry_run_changes_nothing(home):
    directory = write_skill(home, "made-up")
    age(home, "made-up", days=200)

    result = curator.sweep(discovered(home), home, SETTINGS, dry_run=True)

    assert result.archived == 1
    assert directory.exists()
    assert skill_usage.record_for(home, "made-up")["state"] != skill_usage.ARCHIVED


def test_a_skill_you_wrote_is_not_swept(home):
    directory = write_skill(home, "mine", by_agent=False)
    age(home, "mine", days=500)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert result.checked == 0
    assert directory.exists()


def test_the_sweep_summarises_itself(home):
    write_skill(home, "one")
    write_skill(home, "two")
    age(home, "one", days=45)
    age(home, "two", days=200)

    result = curator.sweep(discovered(home), home, SETTINGS)

    assert "marked stale" in result.summary()
    assert "archived" in result.summary()


# ---------------------------------------------------------------------------
# when it runs
# ---------------------------------------------------------------------------


def test_the_first_look_never_sweeps(home):
    """A library that predates this feature has no history, so an immediate
    pass would read every skill as untouched since the epoch."""
    assert curator.due(home, SETTINGS) is False
    assert curator.load_state(home)["last_run_at"]


def test_it_is_due_after_an_interval(home):
    curator.due(home, SETTINGS)  # seeds the clock
    state = curator.load_state(home)
    state["last_run_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(days=10)
    ).isoformat()
    curator.save_state(home, state)

    assert curator.due(home, SETTINGS) is True


def test_it_is_not_due_before_the_interval(home):
    curator.due(home, SETTINGS)
    state = curator.load_state(home)
    state["last_run_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(days=2)
    ).isoformat()
    curator.save_state(home, state)

    assert curator.due(home, SETTINGS) is False


def test_pausing_stops_it(home):
    curator.set_paused(home, True)
    assert curator.is_paused(home) is True
    assert curator.due(home, SETTINGS) is False
    curator.set_paused(home, False)
    assert curator.is_paused(home) is False


def test_turning_it_off_stops_it(home):
    off = curator.Settings(enabled=False)
    assert curator.due(home, off) is False


def test_a_sweep_records_when_it_ran(home):
    write_skill(home, "made-up")
    curator.sweep(discovered(home), home, SETTINGS)
    assert curator.load_state(home)["last_run_at"]
    assert curator.load_state(home)["last_summary"]


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_settings_come_from_the_config():
    settings = curator.Settings.from_config(
        {"curator": True, "curator_stale_days": 10, "curator_archive_days": 40}
    )
    assert (settings.stale_after_days, settings.archive_after_days) == (10, 40)


def test_nonsense_settings_fall_back():
    settings = curator.Settings.from_config(
        {"curator_stale_days": "soon", "curator_archive_days": -4}
    )
    assert settings.stale_after_days == curator.DEFAULT_STALE_AFTER_DAYS
    assert settings.archive_after_days == curator.DEFAULT_ARCHIVE_AFTER_DAYS


def test_archiving_sooner_than_stale_is_corrected():
    """Otherwise the warning state is skipped entirely — and a bad number must
    not stop the sweep running at all."""
    settings = curator.Settings.from_config(
        {"curator_stale_days": 30, "curator_archive_days": 10}
    )
    assert settings.archive_after_days > settings.stale_after_days


# ---------------------------------------------------------------------------
# the review
# ---------------------------------------------------------------------------


def test_the_review_context_carries_what_is_needed_to_judge(home):
    write_skill(home, "made-up", body="Open the thing and read it.")
    skill_usage.seed(home, "made-up")

    context = curator.review_context(discovered(home), home)

    assert "made-up" in context
    assert "Open the thing" in context
    assert "never used" in context


def test_a_long_body_is_clipped(home):
    write_skill(home, "made-up", body="x" * 5000)
    skill_usage.seed(home, "made-up")
    assert "clipped" in curator.review_context(discovered(home), home)


def test_proposals_are_parsed():
    text = '{"proposals": [{"skill": "a", "kind": "merge", "what": "fold into b", "why": "same trigger"}]}'
    proposals = curator.parse_proposals(text)
    assert len(proposals) == 1
    assert proposals[0].skill == "a"
    assert proposals[0].what == "fold into b"


def test_proposals_survive_a_code_fence():
    text = '```json\n{"proposals": [{"skill": "a", "what": "do it"}]}\n```'
    assert curator.parse_proposals(text)[0].what == "do it"


def test_a_proposal_with_no_action_is_dropped():
    """The one thing the instruction asked for none of: a sentence about the
    library rather than something to do."""
    text = '{"proposals": [{"skill": "a", "why": "it is fine"}]}'
    assert curator.parse_proposals(text) == []


def test_unparseable_output_is_no_proposals():
    assert curator.parse_proposals("I had a look and it seems fine!") == []
    assert curator.parse_proposals("") == []


def test_an_empty_list_is_a_complete_answer():
    assert curator.parse_proposals('{"proposals": []}') == []


def test_the_review_writes_its_proposals_down(home):
    write_skill(home, "made-up")
    skill_usage.seed(home, "made-up")

    proposals = curator.review(
        discovered(home),
        home,
        lambda prompt: '{"proposals": [{"skill": "made-up", "what": "say when to use it"}]}',
    )

    assert len(proposals) == 1
    written, stored = curator.load_proposals(home)
    assert written
    assert stored[0].what == "say when to use it"


def test_the_review_changes_nothing_itself(home):
    """An agent may propose; only a person grants."""
    directory = write_skill(home, "made-up", body="original")
    skill_usage.seed(home, "made-up")

    curator.review(
        discovered(home),
        home,
        lambda prompt: '{"proposals": [{"skill": "made-up", "what": "rewrite it"}]}',
    )

    assert (directory / "SKILL.md").read_text().count("original") == 1


def test_a_failed_review_is_not_a_failed_session(home):
    write_skill(home, "made-up")
    skill_usage.seed(home, "made-up")

    def broken(_prompt):
        raise RuntimeError("the provider fell over")

    assert curator.review(discovered(home), home, broken) == []


def test_a_review_of_nothing_asks_nothing(home):
    asked: list[str] = []
    curator.review({}, home, lambda prompt: asked.append(prompt) or "")
    assert asked == []


def test_proposals_can_be_cleared(home):
    curator.save_proposals(home, [curator.Proposal("a", "merge", "do it", "")])
    curator.clear_proposals(home)
    assert curator.load_proposals(home) == ("", [])


def test_a_corrupt_proposals_file_reads_as_none(home):
    curator.proposals_path(home).write_text("{not json", encoding="utf-8")
    assert curator.load_proposals(home) == ("", [])
