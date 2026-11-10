"""Prometheus metrics of the scheduler itself (prefix ``rustic_scheduler_``).

Labels are limited to backup_job, operation and small fixed vocabularies. The
job label is called ``backup_job`` because Prometheus reserves ``job`` for the
scrape job and would otherwise rename it to ``exported_job``. Run ids,
snapshot ids, paths and error texts never become labels; details are available
from ``/api/v1/status`` and the run logs. A value that is unknown is exported as
an absent series, never as zero.
"""

from __future__ import annotations

from collections.abc import Iterable

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

from .model import Operation, OperationResult, Outcome, Progress

_SUMMARY_FIELDS = {
    "files_new": "Files new in the last successful backup",
    "files_changed": "Files changed in the last successful backup",
    "files_unmodified": "Files unmodified in the last successful backup",
    "total_files_processed": "Files processed by the last successful backup",
    "total_bytes_processed": "Bytes processed by the last successful backup",
    "data_added": "Uncompressed bytes added to the repository by the last successful backup",
    "data_added_packed": "Stored bytes added to the repository by the last successful backup",
}


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        jo = ["backup_job", "operation"]

        def gauge(name: str, doc: str, labels: Iterable[str] = ()) -> Gauge:
            return Gauge(f"rustic_scheduler_{name}", doc, list(labels), registry=r)

        def counter(name: str, doc: str, labels: Iterable[str] = ()) -> Counter:
            return Counter(f"rustic_scheduler_{name}", doc, list(labels), registry=r)

        self.info = gauge("info", "Scheduler and Rustic versions", ["version", "rustic_version"])
        self.start_time = gauge("start_time_seconds", "Unix time the scheduler started")
        self.heartbeat = gauge(
            "loop_heartbeat_timestamp_seconds", "Unix time of the last scheduling loop iteration"
        )
        self.ready = gauge("ready", "1 when the scheduler is able to launch scheduled work")
        self.jobs = gauge("jobs", "Number of configured jobs")
        self.state_write_errors = counter(
            "state_write_errors_total", "Failed writes of the state file"
        )

        self.next_run = gauge(
            "next_run_timestamp_seconds", "Unix time of the next scheduled run", jo
        )
        self.running = gauge("operation_running", "1 while the operation is running", jo)
        self.queued = gauge(
            "operation_queued", "1 while the operation waits for its repository", jo
        )
        self.runs = counter(
            "operation_runs_total",
            "Finished or skipped operation runs",
            [*jo, "trigger", "outcome", "reason"],
        )
        self.missed = counter(
            "missed_runs_total", "Scheduled runs that passed while they could not be started", jo
        )
        self.last_outcome = gauge(
            "operation_last_outcome",
            "1 for the outcome of the most recent run, 0 for the other outcomes",
            [*jo, "outcome"],
        )
        self.last_start = gauge(
            "operation_last_start_timestamp_seconds", "Unix time the most recent run started", jo
        )
        self.last_end = gauge(
            "operation_last_end_timestamp_seconds", "Unix time the most recent run ended", jo
        )
        self.last_success = gauge(
            "operation_last_success_timestamp_seconds",
            "Unix time the most recent successful run ended",
            jo,
        )
        self.last_duration = gauge(
            "operation_last_duration_seconds", "Duration of the most recent run", jo
        )
        self.last_exit_code = gauge(
            "operation_last_exit_code", "Exit status of Rustic in the most recent run", jo
        )

        self.progress_bytes_done = gauge(
            "progress_bytes_done", "Bytes processed in the current phase", jo
        )
        self.progress_bytes_total = gauge(
            "progress_bytes_total", "Bytes to process in the current phase, when known", jo
        )
        self.progress_ratio = gauge(
            "progress_ratio", "Completed fraction (0-1) of the current phase, when known", jo
        )
        self.progress_eta = gauge(
            "progress_eta_seconds", "Estimated seconds left in the current phase, when known", jo
        )
        self.progress_updated = gauge(
            "progress_last_update_timestamp_seconds", "Unix time of the last progress message", jo
        )

        self.backup_summary = {
            field: gauge(f"backup_last_{field}", doc, ["backup_job"])
            for field, doc in _SUMMARY_FIELDS.items()
        }
        self.backup_snapshots_saved = gauge(
            "backup_last_snapshots_saved",
            "Snapshots written by the last successful backup",
            ["backup_job"],
        )

        self.snapshot_count = gauge("snapshots", "Snapshots in the job's scope", ["backup_job"])
        self.snapshot_latest = gauge(
            "snapshot_latest_timestamp_seconds", "Unix time of the newest snapshot", ["backup_job"]
        )
        self.snapshot_oldest = gauge(
            "snapshot_oldest_timestamp_seconds", "Unix time of the oldest snapshot", ["backup_job"]
        )
        self.snapshot_latest_bytes = gauge(
            "snapshot_latest_bytes", "Bytes processed by the newest snapshot", ["backup_job"]
        )
        self.snapshot_collected = gauge(
            "snapshot_collection_timestamp_seconds",
            "Unix time snapshot telemetry was last collected successfully",
            ["backup_job"],
        )
        self.snapshot_errors = counter(
            "snapshot_collection_errors_total",
            "Failed snapshot telemetry collections",
            ["backup_job"],
        )

        self.forget_removed = gauge(
            "forget_last_removed", "Snapshots removed (or to remove, in a dry run)", ["backup_job"]
        )
        self.forget_kept = gauge(
            "forget_last_kept", "Snapshots kept by the last forget", ["backup_job"]
        )
        self.forget_dry_run = gauge(
            "forget_last_dry_run", "1 when the last forget was a preview", ["backup_job"]
        )

        self.pushgateway_verified = gauge(
            "pushgateway_push_verified",
            "1 when Rustic's metrics of the last backup were found in the Pushgateway, else 0",
            ["backup_job"],
        )
        self.pushgateway_checks = counter(
            "pushgateway_verifications_total",
            "Pushgateway delivery checks",
            ["backup_job", "result"],
        )
        self.pushgateway_last_push = gauge(
            "pushgateway_last_push_timestamp_seconds",
            "Push time the Pushgateway reports for the job's group",
            ["backup_job"],
        )

        self.mqtt_connected = gauge("mqtt_connected", "1 while connected to the MQTT broker")
        self.mqtt_messages = counter("mqtt_messages_total", "MQTT messages by result", ["result"])
        self.notifications = counter(
            "notifications_total", "Notification attempts by event and result", ["event", "result"]
        )
        self.notification_last_failure = gauge(
            "notification_last_failure_timestamp_seconds",
            "Unix time of the last failed notification",
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)

    # -- helpers ---------------------------------------------------------

    def declare(self, job: str, operation: Operation) -> None:
        """Make the idle series of an enabled operation visible from the start."""
        self.running.labels(job, operation).set(0)
        self.queued.labels(job, operation).set(0)

    def clear_progress(self, job: str, operation: Operation) -> None:
        for metric in (
            self.progress_bytes_done,
            self.progress_bytes_total,
            self.progress_ratio,
            self.progress_eta,
            self.progress_updated,
        ):
            _drop(metric, job, operation)

    def set_progress(self, job: str, operation: Operation, progress: Progress) -> None:
        values = (
            (self.progress_bytes_done, progress.bytes_done),
            (self.progress_bytes_total, progress.bytes_total),
            (self.progress_ratio, progress.ratio),
            (self.progress_eta, progress.eta_seconds),
        )
        for metric, value in values:
            if value is None:
                _drop(metric, job, operation)
            else:
                metric.labels(job, operation).set(value)
        self.progress_updated.labels(job, operation).set(progress.updated_at.timestamp())

    def set_last_result(self, result: OperationResult) -> None:
        """Gauges describing the most recent run (also used to restore state)."""
        job, operation = result.job, result.operation
        for outcome in Outcome:
            self.last_outcome.labels(job, operation, outcome).set(
                1 if outcome == result.outcome else 0
            )
        if result.outcome == Outcome.SKIPPED:
            return
        if result.started_at:
            self.last_start.labels(job, operation).set(result.started_at.timestamp())
        if result.finished_at:
            self.last_end.labels(job, operation).set(result.finished_at.timestamp())
            if result.outcome == Outcome.SUCCEEDED:
                self.last_success.labels(job, operation).set(result.finished_at.timestamp())
        if result.duration_seconds is not None:
            self.last_duration.labels(job, operation).set(result.duration_seconds)
        if result.exit_code is None:
            _drop(self.last_exit_code, job, operation)
        else:
            self.last_exit_code.labels(job, operation).set(result.exit_code)
        if result.outcome != Outcome.SUCCEEDED:
            return
        data = result.data
        if operation == Operation.BACKUP:
            for field, metric in self.backup_summary.items():
                if field in data:
                    metric.labels(job).set(data[field])
            if "snapshots_saved" in data:
                self.backup_snapshots_saved.labels(job).set(data["snapshots_saved"])
        elif operation == Operation.FORGET:
            self.forget_removed.labels(job).set(data.get("removed", 0))
            self.forget_kept.labels(job).set(data.get("kept", 0))
            self.forget_dry_run.labels(job).set(1 if result.dry_run else 0)
        elif operation == Operation.SNAPSHOTS:
            self.snapshot_count.labels(job).set(data.get("count", 0))
            for metric, key in (
                (self.snapshot_latest, "latest"),
                (self.snapshot_oldest, "oldest"),
                (self.snapshot_latest_bytes, "latest_bytes"),
            ):
                if data.get(key) is None:
                    _drop(metric, job)
                else:
                    metric.labels(job).set(data[key])
            if result.finished_at:
                self.snapshot_collected.labels(job).set(result.finished_at.timestamp())

    def count_result(self, result: OperationResult) -> None:
        self.runs.labels(
            result.job, result.operation, result.trigger, result.outcome, result.reason
        ).inc()
        if result.operation == Operation.SNAPSHOTS and result.outcome not in (
            Outcome.SUCCEEDED,
            Outcome.SKIPPED,
        ):
            self.snapshot_errors.labels(result.job).inc()


def _drop(metric: Gauge, *labels: str) -> None:
    try:
        metric.remove(*labels)
    except KeyError:
        pass
