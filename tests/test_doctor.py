from __future__ import annotations

from andromeda_cli.commands import doctor


def test_doctor_reports_without_failing(capsys):
    assert doctor.run() == 0
    out = capsys.readouterr().out
    for label in ("python", "provider", "approval", "browser", "skills", "sessions"):
        assert label in out


def test_doctor_reports_an_unpaired_account(capsys):
    doctor.run()
    assert "not signed in" in capsys.readouterr().out


def test_doctor_reports_a_paired_account(capsys):
    from andromeda_cli import config as config_module

    config_module.save_credentials(
        config_module.Credentials(
            device_token="t" * 64, device_id="cli-1", user_id="user_9", base_url="https://x"
        )
    )
    doctor.run()
    out = capsys.readouterr().out
    assert "user_9" in out
    # The token itself is never printed, by any command.
    assert "t" * 64 not in out


# ---------------------------------------------------------------------------
# `--cloud`: could this container actually do the job
#
# A different question from "is this install healthy", and the difference is the
# point. Nobody is at a terminal inside a runner, so every one of these has to
# be answered before a job runs rather than discovered from five identical
# failures and an auto-pause.
# ---------------------------------------------------------------------------


def test_the_cloud_check_fails_when_home_is_not_the_mounted_volume(capsys, monkeypatch):
    """State on the image layer is discarded on the next boot.

    The symptom is not an error. It is a monitor that re-reports what it already
    reported, forever, at the cost of a model turn each time — which reads as
    "the watched thing keeps changing" rather than as a broken mount.
    """
    assert doctor.run(cloud=True) == 1
    out = capsys.readouterr().out
    assert "home on volume" in out
    assert "not under /data" in out


def test_the_cloud_check_refuses_a_model_key_in_the_environment(capsys, monkeypatch):
    """A container holding a provider key is a key on hardware the user does
    not control, sitting next to agent-authored scripts."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-not-a-real-key")
    assert doctor.run(cloud=True) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().out


def test_the_cloud_check_names_the_binaries_a_job_shells_out_to(capsys):
    """`git` and `rg` work on a Mac and are absent from a slim image.

    A job that calls one fails the moment the runner tries it and works every
    time you test it by hand — invisible exactly while you are looking at it.
    """
    doctor.run(cloud=True)
    out = capsys.readouterr().out
    assert "git present" in out
    assert "rg present" in out


def test_the_plain_check_says_nothing_about_the_cloud(capsys):
    """It is opt-in. A person on a laptop is not running a hosted runner, and a
    screen of red about a volume they do not have teaches them to ignore it."""
    doctor.run()
    assert "as a hosted runner" not in capsys.readouterr().out


def test_the_cloud_check_exits_zero_when_the_runner_is_sound(capsys, monkeypatch, tmp_path):
    """The positive case, so the exit code means something in both directions.

    A check that can only fail is as useless as one that can only pass, and a
    reviewer has no way to tell the two apart from a red screen.
    """
    import os

    from andromeda_cli import config as config_module

    volume = tmp_path / "data"
    volume.mkdir()
    monkeypatch.setenv("ANDROMEDA_HOME", str(volume))
    monkeypatch.setenv("ANDROMEDA_CLOUD_VOLUME", str(volume))
    for name in list(os.environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    (volume / "config.yaml").write_text("provider: relay\n", encoding="utf-8")
    assert config_module.home() == volume

    assert doctor.run(cloud=True) == 0
    out = capsys.readouterr().out
    assert "not under" not in out
    assert "FOUND" not in out


def test_a_relative_volume_name_is_not_a_prefix_match(capsys, monkeypatch, tmp_path):
    """`/data-scratch` is not `/data`, and a naive startswith says it is.

    The failure it would cause is the quiet one: a container mounted at the
    wrong path passes its own boot check and then loses every monitor baseline
    it writes.
    """
    import os

    volume = tmp_path / "data"
    volume.mkdir()
    decoy = tmp_path / "data-scratch"
    decoy.mkdir()
    monkeypatch.setenv("ANDROMEDA_HOME", str(decoy))
    monkeypatch.setenv("ANDROMEDA_CLOUD_VOLUME", str(volume))
    for name in list(os.environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name, raising=False)

    assert doctor.run(cloud=True) == 1
    assert "not under" in capsys.readouterr().out
