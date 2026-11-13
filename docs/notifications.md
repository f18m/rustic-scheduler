# Notifications

Off by default. Notifications are sent with the [Apprise](https://github.com/caronc/apprise)
Python library, in-process. No Apprise API service is needed.

```yaml
notifications:
  enabled: true
  config_files: [/run/secrets/apprise.conf]
  events: [backup_success, backup_failure, maintenance_failure]
```

## Targets

Targets are **not** written in the scheduler's YAML, because Apprise URLs contain tokens and
passwords. They live in one or more Apprise configuration files, mounted as secrets. Both
Apprise formats work: TEXT (one URL per line) and YAML. Example
([examples/secrets/apprise.conf.example](../examples/secrets/apprise.conf.example)):

```
tgram://BOT-TOKEN/CHAT-ID
mailtos://USER:PASSWORD@smtp.example.org?to=you@example.org
```

URL syntax for every supported service: <https://github.com/caronc/apprise/wiki>. The files
are read again for each notification, so changed or rotated targets apply without a restart.

If you already run an [Apprise API](https://github.com/caronc/apprise-api) server, point a
target at it with Apprise's own `apprise://` or `apprises://` URL.

## Events

| Event | Sent when | Default |
|---|---|---|
| `backup_success` | A backup succeeded | on |
| `backup_failure` | A backup ended `failed`, `interrupted` or `unknown` | on |
| `backup_skipped` | A backup was skipped (overlap, busy repository, missed) | off |
| `maintenance_failure` | `forget`, `prune`, `check` or snapshot telemetry ended `failed`, `interrupted` or `unknown` | on |
| `maintenance_success` | One of those succeeded | off |

## Content

Title: `<operation> <outcome>: <job>`, for example `backup failed: documents`.

Body: job, operation, outcome, reason, trigger, duration; for failures Rustic's exit status
and the last error lines; for a succeeded backup the file counts and bytes added; for forget
the number of snapshots removed and kept. Apprise's notification type is `success`, `warning`
(skipped) or `failure`.

The body never contains command lines, environment variables or notification URLs, and passes
through the same secret masking as the logs.

## Delivery, retries, failures

- Sending happens in the background after the result is recorded. It cannot delay the next
  operation.
- Each attempt has `timeout` (default 30s). A failed attempt is repeated up to `retries`
  times (default 2), `retry_delay` apart (default 15s).
- Apprise reports success only if every configured target accepted the message; otherwise the
  attempt counts as failed and is retried for all targets, so a target that did work may
  receive the message again.
- At shutdown, notifications in flight get up to 10 seconds; later ones are lost.

**A failed notification never changes a Rustic outcome.** A backup that succeeded stays
`succeeded`. The failure is visible in:

- `rustic_scheduler_notifications_total{event="...", result="failed"}`
- `rustic_scheduler_notification_last_failure_timestamp_seconds`
- a warning in the log, with URLs masked

Since a broken notification channel is silent by nature, alert on these metrics from
Prometheus as well (see `RusticNotificationsFailing` in the example rules).
