from socialops.cli import main


def test_check_config(real_config_path, capsys):
    assert main(["--config", str(real_config_path), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "2 brands" in out


def test_status_defaults_to_dry_run(real_config_path, capsys):
    main(["--config", str(real_config_path), "status"])
    assert "DRY-RUN" in capsys.readouterr().out


def test_live_flag_reports_live(real_config_path, capsys):
    main(["--config", str(real_config_path), "--live", "status"])
    assert "LIVE" in capsys.readouterr().out


def test_migrate_via_cli(real_config_path, tmp_db, capsys):
    assert main(["--config", str(real_config_path), "--db", str(tmp_db), "migrate"]) == 0
    assert main(["--config", str(real_config_path), "--db", str(tmp_db), "migrate", "--down-to", "0"]) == 0
    out = capsys.readouterr().out
    assert "applied: [1, 2, 3]" in out and "rolled back: [3, 2, 1]" in out


def test_bad_config_exit_code(tmp_path, capsys):
    p = tmp_path / "x.toml"
    p.write_text('timezone = "America/New_York"\n[brands.bluepal_fox]\ndisplay_name="x"\nwebsite="x"\nassets_subdir="x"\n')
    assert main(["--config", str(p), "check-config"]) == 2
