# MQTT

Off by default. When `mqtt.enabled` is false the scheduler opens no connection and nothing
depends on a broker.

Two further features are off unless enabled separately:

- [Commands](#commands): start a backup, check, forget (or snapshots, prune) by MQTT message.
- [Home Assistant discovery](#home-assistant): sensors and buttons appear in Home Assistant
  without any YAML there.

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
| `<prefix>/jobs/<job>/<operation>/state` | yes | `mqtt.qos` | `idle`, `queued` or `running` (plain text) |
| `<prefix>/jobs/<job>/<operation>/run` | – | – | **Subscribed to**, not published: see [Commands](#commands) |
| `<prefix>/jobs/<job>/<operation>/event` | no | `mqtt.qos` | Lifecycle event (JSON) |
| `<prefix>/jobs/<job>/<operation>/progress` | no | 0 | Live progress (JSON) |
| `<prefix>/jobs/<job>/<operation>/result` | yes | `mqtt.qos` | Last result (JSON) |

`offline` is published on a clean stop and, as the connection's last will, when the
scheduler disappears.

### State

`idle`, `queued` (waiting for the repository) or `running`, published when it changes. At
start-up every enabled operation is set to `idle`, which replaces a `running` left behind by
a crash. While `<prefix>/status` is `offline` the retained state is not being maintained.

### Events

```json
{"event": "started", "job": "documents", "operation": "backup", "at": "2026-11-10T01:30:00.012+00:00", "trigger": "schedule"}
```

`event` is `queued`, `started`, `finished`, `skipped` or `rejected`. `finished` and `skipped`
carry the full result (below). `rejected` answers a [command](#commands) that was refused and
carries the reason in `detail`. `trigger` is `schedule`, `catch_up`, `manual`, `mqtt`, `web` or
`after_backup`.

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
(Unix seconds), `latest_id`, `latest_bytes` and `latest_files` (total size and number of
files in the newest snapshot, `null` when Rustic recorded none); for `forget` `removed`, `kept`, `removed_ids`.
Skips are published as events only, so the retained result is always a run that started.

Payloads pass through the same secret masking as the logs.

## Commands

```yaml
mqtt:
  enabled: true
  commands:
    enabled: true
    operations: [backup, check, forget]   # default: [backup, check]
```

Publish the payload `run` to `<prefix>/jobs/<job>/<operation>/run`:

```sh
mosquitto_pub -h mosquitto -u someone -P ... -q 1 \
  -t rustic-scheduler/jobs/documents/backup/run -m run
```

The run is queued exactly like `rustic-scheduler trigger`: it waits for the repository, a
backup is followed by its after-backup sequence, and the result has `"trigger": "mqtt"`.
There is no reply topic. Watch `.../event` (`queued`, then `started`, `finished`; or
`rejected` with the reason), `.../state` and `.../result`.

| Message | What happens |
|---|---|
| Payload `run` (any case, surrounding whitespace ignored), not retained | The operation is queued. |
| Operation not in `mqtt.commands.operations` | `rejected` event. Nothing runs. |
| `forget` or `prune` not enabled on the job | `rejected` event. Listing it under `commands` does not enable it. |
| Same job and operation already queued or running | `rejected` event. Not recorded as a skipped run, so a button pressed twice does not look like a missed backup. |
| **Retained** message | Ignored, with a warning. Otherwise a message left on the broker would start a run at every reconnect. |
| Any other payload, unknown job, unknown operation | Ignored, with a warning. |

Counted in `rustic_scheduler_mqtt_commands_total{result="accepted"|"rejected"|"invalid"}`.

Things to know before enabling it:

- **Anyone who may publish to that topic can start these operations.** MQTT has no notion of
  who sent a message. Use broker authentication, an ACL that limits who may write to
  `<prefix>/jobs/+/+/run`, and TLS. Without TLS the broker password crosses the network in
  clear text.
- **`forget` and `prune` delete data, so they are not in the default list.** A forget started
  this way goes through the same preview and safeguards as any other
  ([maintenance.md](maintenance.md#safeguards)), and `dry_run: true` still applies.
- **An operation can be on-request only.** `forget`, `check` or `prune` enabled with neither
  `after_backup` nor a `schedule` is normally a configuration error; it is accepted when the
  operation is listed in `mqtt.commands.operations`.
- Commands sent while the scheduler is disconnected or stopped are lost: the session is not
  persistent, so the broker does not keep them. This is deliberate; a backlog of old commands
  should not run after an outage.
- QoS 1 can deliver a message twice. The second copy is refused as "already queued or
  running" or, if the first run already finished, starts another run.

## Home Assistant

```yaml
mqtt:
  enabled: true
  homeassistant:
    enabled: true
    discovery_prefix: homeassistant   # Home Assistant's default
    node_id: rustic_scheduler         # change it when running several schedulers
    device_name: Rustic Scheduler
```

Uses Home Assistant's [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).
One device per job, named `<device_name> <job>`, with these entities for each **enabled**
operation (`backup` always; `snapshots`, `check`, `forget`, `prune` when enabled on the job):

| Entity | Source topic | Notes |
|---|---|---|
| `<Operation> state` (sensor) | `.../state` | `idle`, `queued`, `running` |
| `<Operation> last outcome` (sensor) | `.../result` | `succeeded`, `failed`, `interrupted`, `unknown`; attributes `reason`, `detail`, `trigger`, `exit_code`, `attempts`, `dry_run`, `started_at` |
| `<Operation> last finished` (timestamp sensor) | `.../result` | |
| `<Operation> last duration` (sensor, seconds) | `.../result` | |
| `Run <operation>` (button) | publishes `run` to `.../run` | Only with `mqtt.commands.enabled` and the operation listed there |
| `Backup progress` (sensor, %) | `.../backup/progress` | Unknown when Rustic reports no ratio; unavailable when no progress arrived for a while |
| `Backup data added` (sensor, bytes) | `.../backup/result` | |
| `Snapshot count` (sensor) | `.../snapshots/result` | |
| `Latest snapshot size`, `Latest snapshot files` (sensors) | `.../snapshots/result` | Total size (bytes) and number of files in the job's newest snapshot; unknown when Rustic recorded none. Need snapshot telemetry enabled on the job. |

All entities become unavailable when `<prefix>/status` is `offline`.

Checked once by hand against Home Assistant 2026.11.1; see
[verification.md](verification.md). It is not part of the automated tests.

- Discovery messages are retained and published again at every connection, so they survive a
  broker restart and Home Assistant finds them whenever it starts.
- The outcome sensors need `mqtt.retain_results: true` (the default) to show a value after
  Home Assistant restarts.
- A skipped run is an event, not a result, so it does not change `last outcome`. Use the
  Prometheus alerts for missed backups.
- Entity identity is `<node_id>_<job>_<operation>_<entity>`, with `.` in a job name replaced
  by `_`. Two jobs that would collide are a configuration error.
- **Removal.** Entities of an operation you disable are removed at the next start. Entities
  of a job you delete, and all of them if you turn discovery off, stay in Home Assistant:
  delete the device there, and clear the retained messages, for example
  `mosquitto_sub -t 'homeassistant/+/rustic_scheduler/#' --remove-retained -W 2`.

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
  `mqtt.max_reconnect_delay` (default 60s), forever. A lost connection is noticed within
  about one and a half `mqtt.keepalive` periods, also while nothing is being published. After
  reconnecting, the command subscription and the discovery messages are restored.

## Failures

MQTT problems never change a Rustic outcome and never delay a backup. They are visible in:

- `rustic_scheduler_mqtt_connected` (0 or 1)
- `rustic_scheduler_mqtt_messages_total{result="published"|"dropped"|"connection_error"}`
- `rustic_scheduler_mqtt_commands_total{result="accepted"|"rejected"|"invalid"}`
- warnings in the log
