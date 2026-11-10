# MQTT

Off by default. When `mqtt.enabled` is false the scheduler opens no connection and nothing
depends on a broker.

```yaml
mqtt:
  enabled: true
  host: mosquitto
  username: rustic-scheduler
  password_file: /run/secrets/mqtt_password
  topic_prefix: rustic-scheduler
  tls:
    enabled: true
    ca_file: /etc/rustic-scheduler/mqtt-ca.pem
```

## Topics

`<prefix>` is `mqtt.topic_prefix`, `<job>` the job name, `<operation>` one of `backup`,
`snapshots`, `forget`, `prune`, `check`.

| Topic | Retained | QoS | Payload |
|---|---|---|---|
| `<prefix>/status` | yes | 1 | `online` or `offline` (plain text) |
| `<prefix>/jobs/<job>/<operation>/event` | no | `mqtt.qos` | Lifecycle event (JSON) |
| `<prefix>/jobs/<job>/<operation>/progress` | no | 0 | Live progress (JSON) |
| `<prefix>/jobs/<job>/<operation>/result` | yes | `mqtt.qos` | Last result (JSON) |

`offline` is published on a clean stop and, as the connection's last will, when the
scheduler disappears.

### Events

```json
{"event": "started", "job": "documents", "operation": "backup", "at": "2026-11-10T01:30:00.012+00:00", "trigger": "schedule"}
```

`event` is `queued`, `started`, `finished` or `skipped`. `finished` and `skipped` carry the
full result (below). `trigger` is `schedule`, `catch_up`, `manual` or `after_backup`.

### Progress

```json
{"updated_at": "2026-11-10T01:31:10+00:00", "elapsed_seconds": 70, "bytes_done": 1048576000, "bytes_total": 5242880000, "ratio": 0.2, "eta_seconds": 280}
```

A value Rustic did not report is `null`, never 0. Progress describes the current phase, not
the whole operation. At most one message per `mqtt.progress_interval`.

### Results

```json
{
  "job": "documents", "operation": "backup", "trigger": "schedule",
  "outcome": "succeeded", "reason": "none",
  "started_at": "2026-11-10T01:30:00.012+00:00", "finished_at": "2026-11-10T01:42:11.530+00:00",
  "duration_seconds": 731.518, "exit_code": 0, "attempts": 1, "dry_run": false,
  "detail": "", "log_file": "/var/lib/rustic-scheduler/logs/documents/20261110T013000.012000Z-backup.log",
  "data": {"files_new": 12, "files_changed": 3, "files_unmodified": 48211, "data_added_packed": 73400320,
           "total_bytes_processed": 5242880000, "snapshots_saved": 1, "snapshot_ids": ["22342e20"],
           "pushgateway": {"result": "delivered", "detail": ""}}
}
```

`outcome` and `reason` use the vocabulary in [behaviour.md](behaviour.md#outcomes). `data`
depends on the operation: the backup summary; for `snapshots` `count`, `latest`, `oldest`
(Unix seconds), `latest_id`, `latest_bytes`; for `forget` `removed`, `kept`, `removed_ids`.
Skips are published as events only, so the retained result is always a run that started.

Payloads pass through the same secret masking as the logs.

## Delivery

- Messages are queued in memory (`mqtt.queue_size`, default 1000) and sent by a background
  task. Publishing never blocks or fails an operation.
- Events and results use `mqtt.qos` (default 1: at least once, duplicates possible). They wait
  in the queue while the broker is unreachable and are sent after reconnecting.
- Progress uses QoS 0 and is dropped while disconnected.
- When the queue is full, new messages are dropped and counted.
- The queue is not persistent: messages still queued when the scheduler stops are lost.
  Retained results are re-established by the next run.

## Connection

- **Authentication**: `username` and `password_file`. The file is read at every connection
  attempt, so a rotated password is picked up at the next reconnect. Client certificates:
  `tls.cert_file` and `tls.key_file`.
- **TLS**: `tls.enabled: true` verifies the broker's certificate and host name against
  `tls.ca_file`, or the system trust store if unset. Default port becomes 8883.
  `tls.insecure: true` disables verification and is meant for test brokers only. Without TLS,
  credentials and payloads cross the network in clear text.
- **Reconnection**: after a failure the scheduler retries after 1s, doubling up to
  `mqtt.max_reconnect_delay` (default 60s), forever.

## Failures

MQTT problems never change a Rustic outcome and never delay a backup. They are visible in:

- `rustic_scheduler_mqtt_connected` (0 or 1)
- `rustic_scheduler_mqtt_messages_total{result="published"|"dropped"|"connection_error"}`
- warnings in the log
