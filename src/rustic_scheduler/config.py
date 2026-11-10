"""YAML configuration model and static validation.

Only scheduler concepts live here. Rustic's own options stay in the user's TOML
profiles and are never mirrored: the few places where the scheduler passes
options through (`extra_args`, `keep`) forward them verbatim.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .durations import parse_duration
from .schedule import DAY_NAMES, Schedule, parse_day, parse_time

Duration = Annotated[timedelta, BeforeValidator(parse_duration)]

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

#: Options the scheduler owns or that would put a secret on the command line
#: (Rustic stores the full command line inside every snapshot).
_FORBIDDEN_ANYWHERE = {
    "-P": "profiles are set with the job's `profiles` list",
    "--use-profile": "profiles are set with the job's `profiles` list",
    "--json-progress": "the scheduler always enables JSON progress",
    "--no-progress": "the scheduler needs progress output",
    "--password": "it would expose the password in the process list and in snapshot metadata; "
    "use `password-file` in a profile",
    "--key": "it would expose the key in the process list; use `key-file` in a profile",
    "--prometheus-pass": "it would expose the password in the process list; "
    "set RUSTIC_PROMETHEUS_PASS in the environment instead",
    "--label": "the snapshot label is set with the job's `label`",
    "--filter-label": "the snapshot scope is set with the job's `label`",
    "--host": "the snapshot host is set with the job's `host`",
    "--filter-host": "the snapshot scope is set with the job's `host`",
    "-n": "use the operation's `dry_run` setting",
    "--dry-run": "use the operation's `dry_run` setting",
}
_FORBIDDEN_PER_OPERATION = {
    "backup": {"--init": "set `allow_init: true` on the job if Rustic may create the repository"},
    "forget": {
        "--prune": "set `forget.prune: true` so that pruning is an explicit decision",
        "--json": "the scheduler always requests JSON from forget",
        "--keep-none": "put `keep-none: true` under `forget.keep`",
    },
    "check": {
        "--read-data": "set `check.read_data: true`",
        "--read-data-subset": "set `check.read_data_subset`",
    },
    "prune": {},
}


class ConfigError(Exception):
    """Raised with every problem found, one per line."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_extra_args(operation: str, args: list[str]) -> list[str]:
    rules = {**_FORBIDDEN_ANYWHERE, **_FORBIDDEN_PER_OPERATION[operation]}
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in rules:
            raise ValueError(f"`{flag}` is not allowed in {operation}.extra_args: {rules[flag]}")
    return args


class ScheduleConfig(_Model):
    """Days of the week and local times, in an explicit named timezone."""

    days: list[str] = Field(min_length=1)
    times: list[str] = Field(min_length=1)
    timezone: str | None = None

    @field_validator("days", mode="before")
    @classmethod
    def _expand_days(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list) and [str(v).lower() for v in value] == ["daily"]:
            return list(DAY_NAMES)
        return value

    @field_validator("times", mode="before")
    @classmethod
    def _listify_times(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate(self) -> ScheduleConfig:
        for day in self.days:
            parse_day(day)
        for at in self.times:
            parse_time(at)
        if self.timezone is not None:
            _zone(self.timezone)
        return self

    def build(self, default_timezone: str | None) -> Schedule:
        name = self.timezone or default_timezone
        if not name:
            raise ValueError(
                "no timezone: set `timezone` in the schedule or `scheduler.timezone` "
                "(an IANA name such as Europe/Rome)"
            )
        return Schedule(
            days=frozenset(parse_day(day) for day in self.days),
            times=tuple(sorted({parse_time(at) for at in self.times})),
            tz=_zone(name),
        )


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown timezone {name!r}; use an IANA name such as Europe/Rome or UTC"
        ) from exc


class _OperationConfig(_Model):
    timeout: Duration
    retries: int = Field(default=0, ge=0, le=10)
    retry_delay: Duration = timedelta(minutes=5)
    extra_args: list[str] = Field(default_factory=list)


class BackupConfig(_OperationConfig):
    timeout: Duration = timedelta(hours=24)

    @field_validator("extra_args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        return _check_extra_args("backup", value)


class SnapshotsConfig(_Model):
    """Read-only telemetry from `rustic snapshots --json`."""

    enabled: bool = False
    after_backup: bool = True
    schedule: ScheduleConfig | None = None
    timeout: Duration = timedelta(minutes=15)
    retries: int = Field(default=0, ge=0, le=10)
    retry_delay: Duration = timedelta(minutes=1)


class ForgetConfig(_OperationConfig):
    """Snapshot retention. Removes snapshots; disabled unless asked for."""

    enabled: bool = False
    timeout: Duration = timedelta(hours=1)
    #: Passed verbatim as `--<key> <value>`; every key must start with `keep-`.
    keep: dict[str, bool | int | str | list[str]] = Field(default_factory=dict)
    #: Use the `[forget]` section of the profiles instead of `keep`.
    rules_from_profile: bool = False
    #: Run `forget --prune`, which also deletes repository data.
    prune: bool = False
    #: Preview only: nothing is removed, the would-be result is reported.
    dry_run: bool = False
    #: Refuse to run if more snapshots than this would be removed in one go.
    max_remove: int | None = Field(default=None, ge=0)
    after_backup: bool = True
    schedule: ScheduleConfig | None = None

    @field_validator("extra_args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        return _check_extra_args("forget", value)

    @model_validator(mode="after")
    def _validate(self) -> ForgetConfig:
        for key in self.keep:
            if not key.startswith("keep-"):
                raise ValueError(
                    f"forget.keep.{key}: only Rustic `keep-*` options belong here "
                    "(for example keep-daily, keep-weekly, keep-within-daily)"
                )
        if not self.enabled:
            return self
        if self.keep and self.rules_from_profile:
            raise ValueError("forget: set either `keep` or `rules_from_profile`, not both")
        if not self.keep and not self.rules_from_profile:
            raise ValueError(
                "forget is enabled without retention rules: add `keep` (for example "
                "`keep-daily: 7`) or set `rules_from_profile: true`"
            )
        if not self.after_backup and self.schedule is None:
            raise ValueError("forget is enabled but has neither `after_backup` nor a `schedule`")
        return self


class PruneConfig(_OperationConfig):
    """Standalone `rustic prune`. Deletes repository data; affects the whole repository."""

    enabled: bool = False
    timeout: Duration = timedelta(hours=12)
    dry_run: bool = False
    schedule: ScheduleConfig | None = None

    @field_validator("extra_args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        return _check_extra_args("prune", value)

    @model_validator(mode="after")
    def _validate(self) -> PruneConfig:
        if self.enabled and self.schedule is None:
            raise ValueError("prune is enabled but has no `schedule`")
        return self


class CheckConfig(_OperationConfig):
    """`rustic check`. Read-only, but `read_data` downloads pack files."""

    enabled: bool = False
    timeout: Duration = timedelta(hours=6)
    read_data: bool = False
    read_data_subset: str | None = None
    after_backup: bool = False
    schedule: ScheduleConfig | None = None

    @field_validator("extra_args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        return _check_extra_args("check", value)

    @model_validator(mode="after")
    def _validate(self) -> CheckConfig:
        if self.enabled and not self.after_backup and self.schedule is None:
            raise ValueError("check is enabled but has neither `after_backup` nor a `schedule`")
        if self.read_data_subset and not self.read_data:
            raise ValueError("check.read_data_subset needs `read_data: true`")
        return self


class JobConfig(_Model):
    profiles: list[str] = Field(min_length=1)
    #: Paths passed to `rustic backup`. Empty means "use the sources in the profiles".
    sources: list[str] = Field(default_factory=list)
    #: Snapshot label that identifies this job's snapshots. Defaults to the job name.
    label: str | None = None
    #: Optional snapshot host name; also narrows the snapshot scope.
    host: str | None = None
    schedule: ScheduleConfig | None = None
    missed_run_policy: Literal["catch_up", "skip"] | None = None
    misfire_grace: Duration | None = None
    #: How long a run may wait for another operation on the same repository.
    repository_wait_timeout: Duration = timedelta(hours=6)
    #: Override the automatically derived repository identity used for locking.
    repository_lock: str | None = None
    allow_init: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    backup: BackupConfig = BackupConfig()
    snapshots: SnapshotsConfig = SnapshotsConfig()
    forget: ForgetConfig = ForgetConfig()
    prune: PruneConfig = PruneConfig()
    check: CheckConfig = CheckConfig()

    @field_validator("sources", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate(self) -> JobConfig:
        for source in self.sources:
            if not source.startswith("/"):
                raise ValueError(f"source {source!r} must be an absolute path")
        if len(set(self.profiles)) != len(self.profiles):
            raise ValueError("the same profile is listed twice")
        for value, what in ((self.label, "label"), (self.host, "host")):
            if value is not None and (not value.strip() or "," in value):
                raise ValueError(f"{what} must be non-empty and must not contain a comma")
        if self.repository_lock is not None and not NAME_RE.match(self.repository_lock):
            raise ValueError("repository_lock may only contain letters, digits, '_', '.' and '-'")
        return self


class SchedulerConfig(_Model):
    #: Default timezone for schedules that do not name one.
    timezone: str | None = None
    state_dir: Path | None = Path("/var/lib/rustic-scheduler")
    missed_run_policy: Literal["catch_up", "skip"] = "catch_up"
    misfire_grace: Duration = timedelta(hours=24)
    #: Time Rustic gets to stop after SIGTERM before it is killed.
    stop_grace: Duration = timedelta(seconds=30)
    run_logs_per_operation: int = Field(default=20, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        if value is not None:
            _zone(value)
        return value


class RusticConfig(_Model):
    #: Path to the Rustic binary. Empty: pick automatically (see docs/security.md).
    binary: str | None = None
    profile_dir: Path = Path("/etc/rustic-scheduler/profiles")
    progress_interval: Duration = timedelta(seconds=5)
    #: Extra environment for every Rustic process. Not for secrets.
    env: dict[str, str] = Field(default_factory=dict)


class HttpConfig(_Model):
    host: str = "0.0.0.0"  # noqa: S104 - a metrics endpoint must be reachable by Prometheus
    port: int = Field(default=9300, ge=1, le=65535)
    #: A loop heartbeat older than this makes /healthz and /readyz fail.
    max_heartbeat_age: Duration = timedelta(seconds=90)


class PushgatewayConfig(_Model):
    """Where Rustic pushes its own completed-backup metrics."""

    url: str | None = None
    #: After a backup, ask the Pushgateway whether the push actually arrived.
    verify: bool = True
    verify_timeout: Duration = timedelta(seconds=10)
    #: Delete older groups of the same job whose grouping labels changed.
    cleanup_stale_groups: bool = False


class MqttTlsConfig(_Model):
    enabled: bool = False
    ca_file: Path | None = None
    cert_file: Path | None = None
    key_file: Path | None = None
    #: Skip certificate verification. For test brokers only.
    insecure: bool = False


class MqttConfig(_Model):
    enabled: bool = False
    host: str = "localhost"
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password_file: Path | None = None
    client_id: str = "rustic-scheduler"
    topic_prefix: str = "rustic-scheduler"
    qos: Literal[0, 1, 2] = 1
    retain_results: bool = True
    keepalive: Duration = timedelta(seconds=60)
    progress_interval: Duration = timedelta(seconds=10)
    connect_timeout: Duration = timedelta(seconds=10)
    max_reconnect_delay: Duration = timedelta(seconds=60)
    queue_size: int = Field(default=1000, ge=1)
    tls: MqttTlsConfig = MqttTlsConfig()

    @model_validator(mode="after")
    def _validate(self) -> MqttConfig:
        if "#" in self.topic_prefix or "+" in self.topic_prefix:
            raise ValueError("mqtt.topic_prefix must not contain wildcards")
        if self.password_file and not self.username:
            raise ValueError("mqtt.password_file needs mqtt.username")
        return self

    @property
    def effective_port(self) -> int:
        return self.port or (8883 if self.tls.enabled else 1883)


NotificationEvent = Literal[
    "backup_success",
    "backup_failure",
    "backup_skipped",
    "maintenance_success",
    "maintenance_failure",
]


class NotificationsConfig(_Model):
    enabled: bool = False
    #: Apprise configuration files (TEXT or YAML). Keep them in mounted secrets.
    config_files: list[Path] = Field(default_factory=list)
    events: list[NotificationEvent] = ["backup_success", "backup_failure", "maintenance_failure"]
    timeout: Duration = timedelta(seconds=30)
    retries: int = Field(default=2, ge=0, le=10)
    retry_delay: Duration = timedelta(seconds=15)

    @model_validator(mode="after")
    def _validate(self) -> NotificationsConfig:
        if self.enabled and not self.config_files:
            raise ValueError("notifications are enabled but `config_files` is empty")
        return self


class Config(_Model):
    scheduler: SchedulerConfig = SchedulerConfig()
    rustic: RusticConfig = RusticConfig()
    http: HttpConfig = HttpConfig()
    pushgateway: PushgatewayConfig = PushgatewayConfig()
    mqtt: MqttConfig = MqttConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    jobs: dict[str, JobConfig] = Field(min_length=1)

    def profile_path(self, name: str) -> Path:
        """Absolute path of a profile named in a job."""
        path = Path(name)
        if not path.is_absolute():
            path = self.rustic.profile_dir / name
        if path.suffix != ".toml":
            path = path.with_name(path.name + ".toml")
        return path

    def label_of(self, job: str) -> str:
        return self.jobs[job].label or job


def _format_validation_error(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(top level)"
        message = item["msg"].removeprefix("Value error, ")
        if item["type"] == "extra_forbidden":
            message = "unknown setting"
        problems.append(f"{location}: {message}")
    return problems


def _semantic_problems(config: Config) -> list[str]:
    problems: list[str] = []
    labels: dict[tuple[str, str | None], str] = {}
    for name, job in config.jobs.items():
        where = f"jobs.{name}"
        if not NAME_RE.match(name):
            problems.append(
                f"{where}: job names may only contain letters, digits, '_', '.' and '-' "
                "(they are used in metric labels, MQTT topics and file names)"
            )
        for profile in job.profiles:
            path = config.profile_path(profile)
            if not path.is_file():
                problems.append(f"{where}.profiles: profile file {path} does not exist")
        schedules = {
            "schedule": job.schedule,
            "snapshots.schedule": job.snapshots.schedule,
            "forget.schedule": job.forget.schedule,
            "prune.schedule": job.prune.schedule,
            "check.schedule": job.check.schedule,
        }
        for key, schedule in schedules.items():
            if schedule is None:
                continue
            try:
                schedule.build(config.scheduler.timezone)
            except ValueError as exc:
                problems.append(f"{where}.{key}: {exc}")
        # Two jobs with the same label and host could not be told apart, so
        # one job's retention would act on the other's snapshots.
        identity = (config.label_of(name), job.host)
        if identity in labels:
            problems.append(
                f"{where}: label {identity[0]!r} is already used by job {labels[identity]!r}; "
                "each job needs its own label so that telemetry and forget stay separate"
            )
        labels[identity] = name
    if config.mqtt.enabled:
        for secret, what in (
            (config.mqtt.password_file, "mqtt.password_file"),
            (config.mqtt.tls.ca_file, "mqtt.tls.ca_file"),
            (config.mqtt.tls.cert_file, "mqtt.tls.cert_file"),
            (config.mqtt.tls.key_file, "mqtt.tls.key_file"),
        ):
            if secret is not None and not secret.is_file():
                problems.append(f"{what}: file {secret} does not exist")
    if config.notifications.enabled:
        for config_file in config.notifications.config_files:
            if not config_file.is_file():
                problems.append(f"notifications.config_files: file {config_file} does not exist")
    return problems


def parse_config(raw: Any) -> Config:
    """Validate already-loaded YAML. Raises :class:`ConfigError`."""
    if not isinstance(raw, dict):
        raise ConfigError(["the configuration must be a YAML mapping with a `jobs` section"])
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    problems = _semantic_problems(config)
    if problems:
        raise ConfigError(problems)
    return config


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"cannot read {path}: {exc.strerror or exc}"]) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path} is not valid YAML: {exc}"]) from exc
    return parse_config(raw)
