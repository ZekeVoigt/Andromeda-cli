from __future__ import annotations

from pathlib import Path

import pytest

from andromeda_tools import skills


def write_skill(root: Path, name: str, body: str = "Do the thing.", front: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    header = front or f'name: {name}\ndescription: "The {name} skill."'
    (directory / "SKILL.md").write_text(f"---\n{header}\n---\n\n{body}\n", encoding="utf-8")
    return directory


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv(skills.ENV_SKILLS_DIR, str(root))
    return root


def test_discovers_skills_and_parses_frontmatter(skills_root):
    write_skill(skills_root, "weather")
    found = skills.discover()
    assert set(found) == {"weather"}
    assert found["weather"].description == "The weather skill."
    assert found["weather"].body == "Do the thing."


def test_bodies_are_not_in_the_manifest(skills_root):
    """The whole point of skill_load: bodies cost tokens on every turn."""
    write_skill(skills_root, "weather", body="SECRET_BODY_TEXT")
    manifest = skills.manifest(skills.discover())
    assert "weather" in manifest
    assert "SECRET_BODY_TEXT" not in manifest


def test_a_directory_without_a_skill_file_is_ignored(skills_root):
    (skills_root / "notaskill").mkdir()
    write_skill(skills_root, "real")
    assert set(skills.discover()) == {"real"}


def test_a_dotted_directory_is_ignored(skills_root):
    write_skill(skills_root, ".hidden")
    write_skill(skills_root, "real")
    assert set(skills.discover()) == {"real"}


def test_broken_frontmatter_still_yields_a_usable_skill(skills_root):
    directory = skills_root / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: [unclosed\n---\n\nInstructions survive.\n", encoding="utf-8"
    )
    found = skills.discover()
    assert "broken" in found
    assert "Instructions survive." in found["broken"].body


def test_a_missing_binary_marks_the_skill_unavailable(skills_root):
    write_skill(
        skills_root,
        "needsbin",
        front=(
            'name: needsbin\ndescription: "x"\n'
            "metadata:\n  andromeda:\n    requires:\n      bins: [\"definitely-not-installed\"]"
        ),
    )
    skill = skills.discover()["needsbin"]
    assert skill.available is False
    assert skill.missing_bins == ["definitely-not-installed"]
    assert "unavailable" in skills.manifest({"needsbin": skill})


def test_loading_an_unavailable_skill_says_so_before_the_instructions(skills_root):
    write_skill(
        skills_root,
        "needsbin",
        body="Run the thing.",
        front=(
            'name: needsbin\ndescription: "x"\n'
            "metadata:\n  andromeda:\n    requires:\n      bins: [\"definitely-not-installed\"]"
        ),
    )
    found = skills.discover()
    result = skills.load_skill(found, "needsbin")
    assert result.content.startswith("[This skill needs")
    assert "Run the thing." in result.content


def test_loading_an_unknown_skill_lists_what_exists(skills_root):
    write_skill(skills_root, "weather")
    result = skills.load_skill(skills.discover(), "nope")
    assert result.ok is False and "weather" in result.content


def test_a_resource_inside_the_skill_directory_loads(skills_root):
    directory = write_skill(skills_root, "weather")
    (directory / "references").mkdir()
    (directory / "references" / "api.md").write_text("API DETAIL", encoding="utf-8")

    result = skills.load_skill(skills.discover(), "weather", "references/api.md")
    assert result.ok and "API DETAIL" in result.content


def test_a_resource_outside_the_skill_directory_is_refused(skills_root, tmp_path):
    write_skill(skills_root, "weather")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    result = skills.load_skill(skills.discover(), "weather", "../../secret.txt")
    assert result.ok is False and "outside" in result.content


def test_a_missing_resource_is_a_result_not_a_raise(skills_root):
    write_skill(skills_root, "weather")
    assert skills.load_skill(skills.discover(), "weather", "nope.md").ok is False


def test_discovery_walks_up_from_the_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv(skills.ENV_SKILLS_DIR, raising=False)
    root = tmp_path / "repo"
    write_skill(root / "skills", "weather")
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)

    assert skills.resolve_skills_dir(deep) == root / "skills"


def test_the_real_repository_skills_are_readable():
    """The format is the repo's, not a Python-only variant."""
    repo = Path(__file__).resolve().parents[2]
    if not (repo / "skills").is_dir():
        pytest.skip("running outside the monorepo checkout")

    found = skills.discover(repo)
    # A floor, not a count. The bundled set is curated — skills get added and
    # dropped — so pinning its size only ever measures how recently somebody
    # edited this line. What has to hold is that whatever ships parses in the
    # repo's own format and carries a description.
    assert found, "the repository ships no readable skills at all"
    assert all(skill.description for skill in found.values())


class TestTheBundledSkillsAreReachable:
    """The skills we ship must resolve for someone who is not standing in the checkout.

    This is the shape of a real bug, not a hypothetical. Every candidate ahead
    of the bundled directory is relative to the user's working directory or
    their home, so an ordinary install — CLI in `~/.andromeda-cli/checkout`,
    user in `~/projects/something` — resolved to nothing at all. The skills were
    published, present on disk, and invisible to everybody who did not happen to
    be working inside the checkout.

    It survived because every test and every development run starts inside a
    tree that has a `skills/` directory to walk up to.
    """

    def test_a_neutral_directory_still_finds_them(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "projects" / "myapp"
        elsewhere.mkdir(parents=True)
        monkeypatch.chdir(elsewhere)
        monkeypatch.delenv(skills.ENV_SKILLS_DIR, raising=False)
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path / "home"))

        found = skills.resolve_skills_dir()
        assert found is not None, "an installed CLI must find the skills it shipped with"
        assert skills._looks_like_skills_dir(found)

    def test_the_bundled_directory_sits_above_the_package(self):
        """Holds in both layouts, which is why it walks rather than assumes."""
        bundled = skills.bundled_skills_dir()
        assert bundled is not None
        # Any shipped skill proves the directory is the real one. Naming a
        # particular skill made this fail the day that skill was dropped, which
        # says nothing about whether the layout resolved.
        assert list(bundled.glob("*/SKILL.md")), f"{bundled} holds no skills"

    def test_a_project_skills_directory_still_wins(self, tmp_path, monkeypatch):
        """Bundled is the floor. Anything nearer the task overrides it."""
        project = tmp_path / "project"
        (project / "skills" / "local-thing").mkdir(parents=True)
        (project / "skills" / "local-thing" / "SKILL.md").write_text(
            "---\nname: local-thing\ndescription: local\n---\nbody\n", encoding="utf-8"
        )
        monkeypatch.chdir(project)
        monkeypatch.delenv(skills.ENV_SKILLS_DIR, raising=False)

        assert skills.resolve_skills_dir() == project / "skills"
