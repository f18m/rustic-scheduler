from pathlib import Path

import pytest

from rustic_scheduler.config import ConfigError, load_config, parse_config


@pytest.fixture
def base(tmp_path):
    (tmp_path / "repo.toml").write_text("[repository]\n")

    def build(job=None, **top):
        return {
            "scheduler": {"timezone": "Europe/Rome"},
            "rustic": {"profile_dir": str(tmp_path)},
            "jobs": {"docs": {"profiles": ["repo"], "sources": ["/data"]} | (job or {})},
            **top,
        }

    return build


def problems(raw) -> str:
    with pytest.raises(ConfigError) as info:
        parse_config(raw)
    return "\n".join(info.value.problems)


def test_minimal_config_is_valid_and_safe_by_default(base):
    job = parse_config(base()).jobs["docs"]
    # Nothing that deletes data, and nothing optional, is on by default.
    assert not job.forget.enabled and not job.forget.prune
    assert not job.prune.enabled and not job.check.enabled and not job.snapshots.enabled
    assert not job.allow_init
    config = parse_config(base())
    assert not config.mqtt.enabled and not config.notifications.enabled
    assert config.pushgateway.url is None
    assert config.label_of("docs") == "docs"


def test_job_description_is_optional_free_text(base):
    assert parse_config(base()).jobs["docs"].description is None
    job = parse_config(base({"description": "Photos — nightly, to the NAS"})).jobs["docs"]
    assert job.description == "Photos — nightly, to the NAS"


def test_schedule_accepts_daily_and_single_values(base):
    config = parse_config(base({"schedule": {"days": "daily", "times": "02:30"}}))
    schedule = config.jobs["docs"].schedule.build(config.scheduler.timezone)
    assert len(schedule.days) == 7 and schedule.tz.key == "Europe/Rome"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"schedule": {"days": ["mon"], "times": ["2:30"]}}, "HH:MM"),
        ({"schedule": {"days": ["moon"], "times": ["02:30"]}}, "unknown day"),
        ({"schedule": {"days": ["mon"], "times": ["02:30"], "timezone": "Mars/Base"}}, "IANA"),
        ({"schedule": {"days": [], "times": ["02:30"]}}, "at least 1"),
        ({"sources": ["relative/path"]}, "absolute path"),
        ({"profiles": []}, "at least 1"),
        ({"profiles": ["missing"]}, "does not exist"),
        ({"profiles": ["repo", "repo"]}, "listed twice"),
        ({"bogus": 1}, "jobs.docs.bogus: unknown setting"),
        ({"backup": {"timeout": "soon"}}, "duration"),
        ({"label": "a,b"}, "comma"),
        ({"description": "two\nlines"}, "one line of text"),
        ({"description": "  "}, "one line of text"),
        ({"description": "x" * 301}, "at most 300"),
        ({"forget": {"enabled": True}}, "without retention rules"),
        ({"forget": {"enabled": True, "keep": {"prune": True}}}, "keep-"),
        (
            {"forget": {"enabled": True, "keep": {"keep-last": 1}, "rules_from_profile": True}},
            "not both",
        ),
        (
            {"forget": {"enabled": True, "keep": {"keep-last": 1}, "after_backup": False}},
            "schedule",
        ),
        ({"prune": {"enabled": True}}, "no `schedule`"),
        ({"check": {"enabled": True}}, "neither"),
        ({"check": {"read_data_subset": "10%"}}, "read_data: true"),
    ],
)
def test_problems_are_reported_with_location_and_advice(base, job, expected):
    text = problems(base(job))
    assert expected in text
    assert "jobs.docs" in text


@pytest.mark.parametrize(
    ("section", "arg", "expected"),
    [
        ("backup", "--password=hunter2", "process list"),
        ("backup", "--label", "job's `label`"),
        ("backup", "-P", "profiles"),
        ("backup", "--init", "allow_init"),
        ("backup", "--dry-run", "dry_run"),
        ("forget", "--prune", "forget.prune: true"),
        ("forget", "--filter-label", "snapshot scope"),
        ("forget", "--keep-none", "forget.keep"),
        ("check", "--read-data", "check.read_data"),
    ],
)
def test_extra_args_cannot_smuggle_owned_or_dangerous_options(base, section, arg, expected):
    extra = {"extra_args": [arg]}
    if section == "forget":
        extra |= {"keep": {"keep-last": 3}}
    assert expected in problems(base({section: extra}))


def test_missing_timezone_is_an_error_not_a_guess(base):
    raw = base({"schedule": {"days": ["mon"], "times": ["02:30"]}})
    del raw["scheduler"]
    assert "no timezone" in problems(raw)


def test_job_names_and_duplicate_labels(base):
    raw = base()
    raw["jobs"]["bad name!"] = {"profiles": ["repo"], "sources": ["/x"]}
    assert "job names may only contain" in problems(raw)
    raw = base()
    raw["jobs"]["other"] = {"profiles": ["repo"], "sources": ["/x"], "label": "docs"}
    assert "already used by job 'docs'" in problems(raw)


def test_all_problems_are_reported_together(base):
    raw = base({"schedule": {"days": ["moon"], "times": ["02:30"]}, "bogus": 1})
    raw["mqtt"] = {"enabled": True, "password_file": "/nonexistent"}
    text = problems(raw)
    assert "unknown day" in text and "bogus" in text and "mqtt" in text


def test_optional_services_need_their_files(base, tmp_path):
    raw = base(mqtt={"enabled": True, "username": "u", "password_file": str(tmp_path / "nope")})
    assert "mqtt.password_file" in problems(raw)
    raw = base(notifications={"enabled": True})
    assert "config_files" in problems(raw)
    raw = base(notifications={"enabled": True, "config_files": [str(tmp_path / "nope")]})
    assert "does not exist" in problems(raw)


def test_mqtt_port_follows_tls(base):
    assert parse_config(base(mqtt={"enabled": True})).mqtt.effective_port == 1883
    tls = parse_config(base(mqtt={"enabled": True, "tls": {"enabled": True}}))
    assert tls.mqtt.effective_port == 8883


def test_load_config_reports_yaml_and_io_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("jobs: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
    bad.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad)


def test_example_config_is_valid(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    import yaml

    raw = yaml.safe_load((root / "examples" / "config.yaml").read_text())
    raw["rustic"] = raw.get("rustic", {}) | {"profile_dir": str(root / "examples" / "profiles")}
    # The example refers to secret files that only exist in a deployment.
    raw.get("mqtt", {}).pop("password_file", None)
    raw.get("mqtt", {}).pop("username", None)
    raw.get("mqtt", {}).get("tls", {}).pop("ca_file", None)
    raw.get("notifications", {})["enabled"] = False
    config = parse_config(raw)
    assert config.jobs
