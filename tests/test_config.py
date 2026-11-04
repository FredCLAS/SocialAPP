import textwrap

import pytest

from socialops.config import ConfigError, load_config, parse_config


def test_real_config_loads(real_config_path):
    cfg = load_config(real_config_path)
    assert set(cfg.brands) == {"clas_by_fred", "pamela_art"}
    assert cfg.dry_run is True
    assert cfg.timezone.key == "America/New_York"


def test_pamela_art_only_fb_and_ig(real_config_path):
    cfg = load_config(real_config_path)
    assert set(cfg.brands["pamela_art"].platforms) == {"facebook", "instagram"}


def test_clas_cadence_matches_spec(real_config_path):
    cfg = load_config(real_config_path)
    clas = cfg.brands["clas_by_fred"].platforms
    assert clas["facebook"].per_week == 7 and clas["facebook"].video_per_week == 3
    assert clas["instagram"].per_week == 7
    assert clas["linkedin"].per_week == 3 and clas["linkedin"].days == ("tue", "wed", "thu")
    assert clas["linkedin"].post_time == "10:00"
    assert clas["youtube"].upload_deadline_hours == 48


def test_outreach_sender_is_classbyfred(real_config_path):
    cfg = load_config(real_config_path)
    assert cfg.brands["clas_by_fred"].outreach_sender == "fred@clasbyfred.com"
    assert cfg.brands["pamela_art"].outreach_sender is None


def test_footer_address_present(real_config_path):
    cfg = load_config(real_config_path)
    assert "St Augustine" in cfg.footer.postal_address


@pytest.mark.parametrize("excluded", ["bluepal_fox", "compliance_without_panic"])
def test_excluded_brand_rejected(excluded):
    data = {"timezone": "America/New_York", "brands": {excluded: {"display_name": "x", "website": "x", "assets_subdir": "x"}}}
    with pytest.raises(ConfigError, match="excluded"):
        parse_config(data)


def test_pamela_cannot_have_youtube():
    data = {
        "timezone": "America/New_York",
        "brands": {
            "pamela_art": {
                "display_name": "P", "website": "w", "assets_subdir": "p",
                "platforms": {"youtube": {"per_week": 1, "days": ["mon"], "post_time": "08:30"}},
            }
        },
    }
    with pytest.raises(ConfigError, match="not allowed"):
        parse_config(data)


def test_cold_sender_must_be_classbyfred():
    data = {
        "timezone": "America/New_York",
        "footer": {"company": "c", "postal_address": "a"},
        "brands": {"clas_by_fred": {"display_name": "c", "website": "w", "assets_subdir": "c",
                                    "outreach_sender": "x@gmail.com"}},
    }
    with pytest.raises(ConfigError, match="clasbyfred.com"):
        parse_config(data)


def test_outreach_requires_footer():
    data = {
        "timezone": "America/New_York",
        "brands": {"clas_by_fred": {"display_name": "c", "website": "w", "assets_subdir": "c",
                                    "outreach_sender": "fred@clasbyfred.com"}},
    }
    with pytest.raises(ConfigError, match="postal address"):
        parse_config(data)


def test_days_must_match_per_week():
    data = {
        "timezone": "America/New_York",
        "brands": {"pamela_art": {"display_name": "P", "website": "w", "assets_subdir": "p",
                                  "platforms": {"facebook": {"per_week": 4, "days": ["mon"], "post_time": "08:30"}}}},
    }
    with pytest.raises(ConfigError, match="exactly per_week"):
        parse_config(data)


def test_bad_time_rejected():
    data = {
        "timezone": "America/New_York",
        "brands": {"pamela_art": {"display_name": "P", "website": "w", "assets_subdir": "p",
                                  "platforms": {"facebook": {"per_week": 1, "days": ["mon"], "post_time": "8:30am"}}}},
    }
    with pytest.raises(ConfigError, match="HH:MM"):
        parse_config(data)


def test_bad_timezone_rejected():
    with pytest.raises(ConfigError, match="timezone"):
        parse_config({"timezone": "Mars/Olympus", "brands": {}})


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text(textwrap.dedent("timezone = "))
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(p)


def test_canva_section_loaded(real_config_path):
    cfg = load_config(real_config_path)
    assert cfg.canva is not None
    assert cfg.canva.redirect_port == 8765
    assert "design:content:write" in cfg.canva.scopes


def test_pamela_contact_and_www_site(real_config_path):
    cfg = load_config(real_config_path)
    pam = cfg.brands["pamela_art"]
    assert pam.contact_email == "pam@pamelapatton.art"
    assert pam.website == "https://www.pamelapatton.art"
    assert pam.outreach_sender is None


def test_canva_requires_scopes():
    data = {"timezone": "America/New_York", "canva": {"authorize_url": "x", "token_url": "y", "scopes": []}}
    with pytest.raises(ConfigError, match="scopes"):
        parse_config(data)
