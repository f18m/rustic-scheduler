import pytest

from rustic_scheduler.commands import (
    backup_command,
    check_command,
    command_for,
    forget_command,
    make_runtime,
    prune_command,
    snapshots_command,
)
from rustic_scheduler.config import parse_config
from rustic_scheduler.model import Operation
from rustic_scheduler.profiles import analyse, repository_lock_key


@pytest.fixture
def build(tmp_path):
    for name in ("repo", "opts"):
        (tmp_path / f"{name}.toml").write_text("")

    def make(job=None, raw=None, **top):
        config = parse_config(
            {
                "rustic": {"profile_dir": str(tmp_path), "progress_interval": "5s"},
                "jobs": {
                    "docs": {"profiles": ["repo", "opts"], "sources": ["/a", "/b"]} | (job or {})
                },
                **top,
            }
        )
        merged = analyse(
            "docs",
            config.jobs["docs"],
            config.label_of("docs"),
            raw or {"repository": {"repository": "/r"}},
        )
        return config, make_runtime(config, "docs", "rustic", merged), tmp_path

    return make


def test_backup_command(build):
    config, runtime, tmp = build()
    assert backup_command(config, runtime) == [
        "rustic",
        "-P",
        f"{tmp}/repo.toml",
        "-P",
        f"{tmp}/opts.toml",
        "--json-progress",
        "--progress-interval",
        "5s",
        "backup",
        "--label",
        "docs",
        "/a",
        "/b",
    ]


def test_profiles_keep_their_order_and_may_be_absolute(build, tmp_path):
    other = tmp_path / "elsewhere.toml"
    other.write_text("")
    config, runtime, tmp = build({"profiles": [str(other), "repo"]})
    assert runtime.base_args == ("-P", str(other), "-P", f"{tmp}/repo.toml")


def test_host_and_label_are_explicit(build):
    config, runtime, _ = build({"label": "nas-docs", "host": "nas"})
    backup = backup_command(config, runtime)
    assert backup[backup.index("--label") + 1] == "nas-docs"
    assert backup[backup.index("--host") + 1] == "nas"
    assert snapshots_command(runtime)[-5:] == [
        "--json",
        "--filter-label",
        "nas-docs",
        "--filter-host",
        "nas",
    ]


def test_profile_progress_interval_is_respected(build):
    raw = {"repository": {"repository": "/r"}, "global": {"progress-interval": "PT2S"}}
    config, runtime, _ = build(raw=raw)
    assert "--progress-interval" not in backup_command(config, runtime)


def test_forget_is_always_scoped_and_never_prunes_unless_asked(build):
    _, runtime, _ = build(
        {"forget": {"enabled": True, "keep": {"keep-daily": 7, "keep-weekly": 4}}}
    )
    real = forget_command(runtime, dry_run=False)
    assert real[real.index("forget") :] == [
        "forget",
        "--json",
        "--filter-label",
        "docs",
        "--keep-daily",
        "7",
        "--keep-weekly",
        "4",
    ]
    assert "--prune" not in real and "--dry-run" not in real
    assert "--dry-run" in forget_command(runtime, dry_run=True)


def test_forget_prune_only_on_a_real_run(build):
    _, runtime, _ = build({"forget": {"enabled": True, "keep": {"keep-last": 3}, "prune": True}})
    assert "--prune" in forget_command(runtime, dry_run=False)
    assert "--prune" not in forget_command(runtime, dry_run=True)


def test_forget_keep_value_types(build):
    keep = {
        "keep-none": True,
        "keep-tags": ["a", "b"],
        "keep-within-daily": "7 days",
        "keep-last": 0,
    }
    _, runtime, _ = build({"forget": {"enabled": True, "keep": keep}})
    args = forget_command(runtime, dry_run=True)
    assert "--keep-none" in args
    assert args.count("--keep-tags") == 2
    assert args[args.index("--keep-within-daily") + 1] == "7 days"


def test_forget_dry_run_setting_drives_command_for(build):
    job = {"forget": {"enabled": True, "keep": {"keep-last": 3}, "dry_run": True, "prune": True}}
    config, runtime, _ = build(job)
    args = command_for(config, runtime, Operation.FORGET)
    assert "--dry-run" in args and "--prune" not in args


def test_check_and_prune_commands(build):
    job = {
        "check": {
            "enabled": True,
            "after_backup": True,
            "read_data": True,
            "read_data_subset": "5%",
        },
        "prune": {
            "enabled": True,
            "schedule": {"days": "daily", "times": "04:00", "timezone": "UTC"},
            "extra_args": ["--max-unused", "10%"],
        },
    }
    config, runtime, _ = build(job)
    check = check_command(config, runtime)
    assert check[check.index("check") :] == ["check", "--read-data", "--read-data-subset", "5%"]
    assert "--json-progress" in check
    assert prune_command(runtime)[-3:] == ["prune", "--max-unused", "10%"]
    config, runtime, _ = build()
    assert check_command(config, runtime)[-1] == "check"


def test_pushgateway_env_only_when_profiles_are_silent(build):
    _, runtime, _ = build(pushgateway={"url": "http://pgw:9091"})
    assert runtime.env["RUSTIC_PROMETHEUS"] == "http://pgw:9091"
    raw = {"repository": {"repository": "/r"}, "global": {"prometheus": "http://mine:9091/"}}
    _, runtime, _ = build(raw=raw, pushgateway={"url": "http://pgw:9091"})
    assert "RUSTIC_PROMETHEUS" not in runtime.env  # the user's own value stays in force
    assert runtime.pushgateway_url == "http://mine:9091/"


def test_env_layers(build, monkeypatch):
    monkeypatch.setenv("INHERITED", "1")
    _, runtime, _ = (
        build({"env": {"B": "job", "C": "job"}}, rustic_extra=None)
        if False
        else build({"env": {"B": "job"}})
    )
    assert runtime.env["INHERITED"] == "1" and runtime.env["B"] == "job"


def test_lock_key_identifies_the_repository_not_the_secrets():
    base = {"repository": {"repository": "opendal:s3", "options": {"bucket": "b", "root": "/x"}}}
    with_secret = {
        "repository": {
            "repository": "opendal:s3",
            "password": "p",
            "options": {
                "bucket": "b",
                "root": "/x",
                "secret_access_key": "s",
                "access_key_id": "k",
            },
        }
    }
    other = {"repository": {"repository": "opendal:s3", "options": {"bucket": "b", "root": "/y"}}}
    assert repository_lock_key(base) == repository_lock_key(with_secret)
    assert repository_lock_key(base) != repository_lock_key(other)
    hot = {"repository": {"repository": "opendal:s3", "repo-hot": "opendal:s3"}}
    assert repository_lock_key(hot) != repository_lock_key(
        {"repository": {"repository": "opendal:s3"}}
    )


def test_lock_key_ignores_options_that_do_not_locate_the_repository():
    def key(repository: str = "opendal:s3", **options: object) -> str:
        return repository_lock_key({"repository": {"repository": repository, "options": options}})

    base = key(bucket="b", root="/x")
    # How Rustic talks to the repository is not which repository it is.
    assert base == key(bucket="b", root="/x", timeout="10min", retry="5", connections="4")
    assert base == key(bucket="b", root="/x", throttle="10kB,10MB", region="eu-west-1")
    assert base == key(bucket="b", root="/x", default_storage_class="DEEP_ARCHIVE")
    # Slashes that do not change the location.
    assert base == key(bucket="b", root="x/")
    assert key(bucket="b") == key(bucket="b", root="/") == key(bucket="b", root="")
    assert key("/srv/repo") == key("/srv/repo/")
    # Location options and the URL still tell repositories apart.
    assert base != key(bucket="c", root="/x")
    assert base != key(bucket="b", root="/x/y")
    assert key("/srv/repo") != key("/srv/other")
    cold = {"repository": "opendal:s3", "options-cold": {"bucket": "cold"}}
    assert repository_lock_key({"repository": cold}) != repository_lock_key(
        {"repository": cold | {"options-cold": {"bucket": "colder"}}}
    )


def test_explicit_lock_name_wins(build):
    _, runtime, _ = build({"repository_lock": "shared-nas"})
    assert runtime.lock_key == "shared-nas"
