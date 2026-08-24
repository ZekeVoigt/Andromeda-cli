"""Profiles: several independent installs, one program.

The isolation is the feature, so most of these assert that something does
*not* cross between two profiles.
"""

from __future__ import annotations

import pytest

from andromeda_cli import config as config_module
from andromeda_cli import profiles
from andromeda_cli import sessions as store
from andromeda_cli import state


@pytest.fixture(autouse=True)
def home_without_an_override(tmp_path, monkeypatch):
    """Profiles only resolve when ANDROMEDA_HOME is not set.

    The suite's `isolated_home` fixture sets it, and an explicit home beats a
    profile on purpose — so these tests move HOME instead.
    """
    monkeypatch.delenv("ANDROMEDA_HOME", raising=False)
    monkeypatch.delenv(profiles.ENV_PROFILE, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)


class TestNames:
    @pytest.mark.parametrize("name", ["work", "a", "a-b_c", "x1", "default"])
    def test_a_usable_name_is_accepted(self, name):
        assert profiles.validate(name) == name

    @pytest.mark.parametrize(
        "name", ["..", "../evil", "a/b", "a\\b", "-lead", "_lead", "", "  ", "a" * 65]
    )
    def test_a_name_that_could_escape_the_home_is_rejected(self, name):
        """Rejected rather than sanitised: a sanitised name silently addresses
        a different profile than the one that was typed."""
        with pytest.raises(profiles.ProfileError):
            profiles.validate(name)

    def test_a_name_is_case_insensitive(self):
        assert profiles.validate("Work") == "work"


class TestResolution:
    def test_the_default_profile_is_the_home_itself(self):
        """Which is what makes this free to add: an existing install is
        already the default profile and there is nothing to migrate."""
        assert profiles.path_for("default") == profiles.default_root()
        assert config_module.home() == profiles.default_root()

    def test_an_explicit_home_beats_a_sticky_profile(self, tmp_path, monkeypatch):
        """It is what a test, a container and a scheduled job use to be
        certain which state they are touching."""
        profiles.create("work")
        profiles.use("work")
        monkeypatch.setenv("ANDROMEDA_HOME", str(tmp_path / "explicit"))
        assert config_module.home() == tmp_path / "explicit"
        assert profiles.selected() == "default"

    def test_the_environment_beats_the_sticky_choice(self, monkeypatch):
        profiles.create("work")
        profiles.create("personal")
        profiles.use("work")
        monkeypatch.setenv(profiles.ENV_PROFILE, "personal")
        assert profiles.selected() == "personal"

    def test_an_unreadable_sticky_file_reads_as_no_choice(self):
        """Rather than breaking every command until it is fixed."""
        profiles.default_root().mkdir(parents=True, exist_ok=True)
        profiles.sticky_path().write_text("../nonsense\n", encoding="utf-8")
        assert profiles.selected() == "default"


class TestLifecycle:
    def test_creating_one_bootstraps_its_directories(self):
        profile = profiles.create("work")
        for name in ("sessions", "memory", "skills", "cron"):
            assert (profile.path / name).is_dir()

    def test_creating_a_duplicate_is_refused(self):
        profiles.create("work")
        with pytest.raises(profiles.ProfileError):
            profiles.create("work")

    def test_the_default_cannot_be_created(self):
        with pytest.raises(profiles.ProfileError):
            profiles.create("default")

    def test_cloning_copies_settings_but_never_credentials(self):
        """A clone is a new install, and it pairs itself."""
        root = profiles.default_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.yaml").write_text("model: test/model\n", encoding="utf-8")
        (root / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
        (root / "credentials.json").write_text('{"device_token": "secret"}', encoding="utf-8")

        profile = profiles.create("work", clone=True)
        assert (profile.path / "config.yaml").exists()
        assert (profile.path / "SOUL.md").exists()
        assert not (profile.path / "credentials.json").exists()

    def test_clone_all_still_excludes_the_token_and_the_index(self):
        root = profiles.default_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "credentials.json").write_text("{}", encoding="utf-8")
        (root / "state.db").write_text("", encoding="utf-8")
        (root / "history").write_text("", encoding="utf-8")
        (root / "config.yaml").write_text("model: test/model\n", encoding="utf-8")

        profile = profiles.create("work", clone_all=True)
        assert (profile.path / "config.yaml").exists()
        for excluded in ("credentials.json", "state.db", "history"):
            assert not (profile.path / excluded).exists()

    def test_using_the_default_removes_the_sticky_file(self):
        """One representation of "no profile selected", not two."""
        profiles.create("work")
        profiles.use("work")
        assert profiles.sticky_path().exists()
        profiles.use("default")
        assert not profiles.sticky_path().exists()

    def test_using_a_profile_that_does_not_exist_is_refused(self):
        with pytest.raises(profiles.ProfileError):
            profiles.use("nope")

    def test_the_default_cannot_be_deleted(self):
        with pytest.raises(profiles.ProfileError):
            profiles.delete("default", force=True)

    def test_the_profile_in_use_is_not_deleted_by_accident(self):
        """Deleting the profile you are standing in leaves a running session
        writing into a directory that no longer exists."""
        profiles.create("work")
        profiles.use("work")
        with pytest.raises(profiles.ProfileError):
            profiles.delete("work")

    def test_deleting_clears_a_sticky_choice_that_pointed_at_it(self):
        profiles.create("work")
        profiles.use("work")
        profiles.delete("work", force=True)
        assert profiles.selected() == "default"
        assert not profiles.exists("work")

    def test_listing_marks_the_current_one(self):
        profiles.create("work")
        profiles.use("work")
        found = {item.name: item for item in profiles.listing()}
        assert found["work"].current and not found["default"].current


class TestIsolation:
    def test_sessions_do_not_cross_between_profiles(self, monkeypatch):
        session = store.Session()
        session.messages = [{"role": "user", "content": "the retry budget"}]
        session.save()
        state.index_session(session)
        assert len(state.search("retry budget")) == 1

        profiles.create("work")
        monkeypatch.setenv(profiles.ENV_PROFILE, "work")
        assert state.search("retry budget") == []
        assert store.recent() == []

    def test_each_profile_gets_its_own_index_file(self, monkeypatch):
        default_db = state.db_path()
        profiles.create("work")
        monkeypatch.setenv(profiles.ENV_PROFILE, "work")
        assert state.db_path() != default_db
