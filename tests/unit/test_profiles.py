import pytest

from rustic_scheduler.config import parse_config
from rustic_scheduler.profiles import analyse


@pytest.fixture
def check(tmp_path):
    (tmp_path / "repo.toml").write_text("")

    def run(raw, job=None):
        config = parse_config(
            {
                "rustic": {"profile_dir": str(tmp_path)},
                "jobs": {"docs": {"profiles": ["repo"], "sources": ["/a"]} | (job or {})},
            }
        )
        merged = analyse("docs", config.jobs["docs"], config.label_of("docs"), raw)
        return "\n".join(merged.problems), "\n".join(merged.warnings)

    return run


REPO = {"repository": {"repository": "/r"}}


def test_clean_profile_has_no_findings(check):
    assert check(REPO) == ("", "")


def test_missing_repository(check):
    assert "no\nrepository" in check({})[0] or "repository to work on" in check({})[0]


def test_label_conflict_is_an_error_with_two_ways_out(check):
    problems, _ = check(REPO | {"backup": {"label": "mine"}})
    assert "label = 'mine'" in problems and "override" in problems and "`label: mine`" in problems
    assert check(REPO | {"backup": {"label": "mine"}}, {"label": "mine"})[0] == ""
    assert check(REPO | {"backup": {"label": "docs"}})[0] == ""


def test_per_snapshot_label_and_host_conflicts(check):
    raw = REPO | {"backup": {"snapshots": [{"sources": ["/x"], "label": "other"}]}}
    assert "[[backup.snapshots]] #1 label" in check(raw)[0]
    raw = REPO | {"backup": {"host": "nas"}}
    assert check(raw)[0] == ""  # the scheduler passes no --host, nothing is overridden
    assert "[backup] host" in check(raw, {"host": "other"})[0]


def test_sources_must_come_from_somewhere(check):
    assert "nothing to back up" in check(REPO, {"sources": []})[0]
    raw = REPO | {"backup": {"snapshots": [{"sources": ["/x"]}]}}
    assert check(raw, {"sources": []})[0] == ""


def test_init_and_dry_run_need_explicit_consent(check):
    assert "allow_init" in check(REPO | {"backup": {"init": True}})[0]
    assert check(REPO | {"backup": {"init": True}}, {"allow_init": True})[0] == ""
    assert "dry-run" in check(REPO | {"global": {"dry-run": True}})[0]


def test_profile_prune_is_refused_unless_enabled_in_yaml(check):
    raw = REPO | {"forget": {"prune": True, "keep-last": 3}}
    assert "prune = true" in check(raw)[0]
    job = {"forget": {"enabled": True, "keep": {"keep-last": 3}}}
    assert "prune = true" in check(raw, job)[0]
    job["forget"]["prune"] = True
    assert check(raw, job)[0] == ""


def test_rules_from_profile_needs_rules(check):
    job = {"forget": {"enabled": True, "rules_from_profile": True}}
    assert "no keep-*" in check(REPO, job)[0]
    assert check(REPO | {"forget": {"keep-daily": 7}}, job)[0] == ""


def test_profile_keeps_combined_with_yaml_keeps_warn(check):
    job = {"forget": {"enabled": True, "keep": {"keep-daily": 7}}}
    problems, warnings = check(REPO | {"forget": {"keep-last": 100, "keep-daily": 3}}, job)
    assert (
        problems == ""
        and "keep-last" in warnings
        and "keep-daily" not in warnings.split("sets")[1].split(";")[0]
    )


def test_profile_filters_that_widen_the_scope_are_refused(check):
    job = {"forget": {"enabled": True, "keep": {"keep-daily": 7}}}
    assert "filter-labels" in check(REPO | {"forget": {"filter-labels": ["other"]}}, job)[0]
    assert "filter-labels" in check(REPO | {"snapshot-filter": {"filter-labels": ["x"]}}, job)[0]
    assert check(REPO | {"forget": {"filter-labels": ["docs"]}}, job)[0] == ""


def test_no_progress_only_warns(check):
    problems, warnings = check(REPO | {"global": {"no-progress": True}})
    assert problems == "" and "no-progress" in warnings
