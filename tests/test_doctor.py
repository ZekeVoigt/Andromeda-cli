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
