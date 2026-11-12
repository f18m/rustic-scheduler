"""Home Assistant MQTT discovery.

Builds the retained discovery messages that make Home Assistant create one
device per job, with sensors fed by the topics the scheduler publishes anyway
and, when MQTT commands are enabled, a button per operation that may be started.

For every job, each possible entity gets either its configuration or an empty
payload. The empty payload removes an entity whose operation was disabled since
the last start. Entities of a job that was deleted from the configuration are
not known here and have to be removed by hand (see docs/mqtt.md).
"""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .config import MqttConfig, entity_slug
from .model import Operation

#: (key, Home Assistant component). Every operation has these.
_COMMON = (
    ("state", "sensor"),
    ("outcome", "sensor"),
    ("finished", "sensor"),
    ("duration", "sensor"),
    ("run", "button"),
)
#: Entities that exist for one operation only.
_EXTRA = {
    Operation.BACKUP: (("progress", "sensor"), ("data_added", "sensor")),
    Operation.SNAPSHOTS: (("count", "sensor"),),
}
_ATTRIBUTES = (
    "{{ {'reason': value_json.reason, 'detail': value_json.detail, "
    "'trigger': value_json.trigger, 'exit_code': value_json.exit_code, "
    "'attempts': value_json.attempts, 'dry_run': value_json.dry_run, "
    "'started_at': value_json.started_at} | tojson }}"
)


def _entity(key: str, base: str, title: str, progress_expiry: int) -> dict[str, Any]:
    result = f"{base}/result"
    match key:
        case "state":
            return {
                "name": f"{title} state",
                "state_topic": f"{base}/state",
                "device_class": "enum",
                "options": ["idle", "queued", "running"],
                "icon": "mdi:progress-clock",
            }
        case "outcome":
            return {
                "name": f"{title} last outcome",
                "state_topic": result,
                "value_template": "{{ value_json.outcome }}",
                "json_attributes_topic": result,
                "json_attributes_template": _ATTRIBUTES,
                "icon": "mdi:clipboard-check-outline",
            }
        case "finished":
            return {
                "name": f"{title} last finished",
                "state_topic": result,
                "value_template": "{{ value_json.finished_at }}",
                "device_class": "timestamp",
            }
        case "duration":
            return {
                "name": f"{title} last duration",
                "state_topic": result,
                "value_template": "{{ value_json.duration_seconds }}",
                "device_class": "duration",
                "unit_of_measurement": "s",
                "state_class": "measurement",
            }
        case "run":
            return {
                "name": f"Run {title.lower()}",
                "command_topic": f"{base}/run",
                "payload_press": "run",
                "qos": 1,
                "icon": "mdi:play-circle-outline",
            }
        case "progress":
            # A ratio Rustic did not report renders as None, which Home Assistant
            # shows as unknown rather than 0.
            return {
                "name": f"{title} progress",
                "state_topic": f"{base}/progress",
                "value_template": (
                    "{{ (value_json.ratio * 100) | round(1) "
                    "if value_json.ratio is not none else None }}"
                ),
                "unit_of_measurement": "%",
                "expire_after": progress_expiry,
                "icon": "mdi:progress-upload",
            }
        case "data_added":
            return {
                "name": f"{title} data added",
                "state_topic": result,
                "value_template": "{{ value_json.data.data_added_packed | default(None) }}",
                "device_class": "data_size",
                "unit_of_measurement": "B",
                "state_class": "measurement",
            }
        case "count":
            return {
                "name": "Snapshot count",
                "state_topic": result,
                "value_template": "{{ value_json.data.count | default(None) }}",
                "state_class": "measurement",
                "icon": "mdi:camera-burst",
            }
    raise ValueError(key)


def discovery_messages(
    mqtt: MqttConfig, operations: dict[str, list[Operation]]
) -> list[tuple[str, str]]:
    """Retained (topic, payload) pairs; `operations` maps each job to its enabled ones."""
    ha = mqtt.homeassistant
    prefix = mqtt.topic_prefix.strip("/")
    discovery = ha.discovery_prefix.strip("/")
    startable = set(mqtt.commands.operations) if mqtt.commands.enabled else set()
    expiry = max(int(mqtt.progress_interval.total_seconds() * 3), 60)
    origin = {
        "name": "rustic-scheduler",
        "sw_version": __version__,
        "support_url": "https://github.com/f18m/rustic-scheduler",
    }
    messages: list[tuple[str, str]] = []
    for job, enabled in operations.items():
        slug = entity_slug(job)
        device = {
            "identifiers": [f"{ha.node_id}_{slug}"],
            "name": f"{ha.device_name} {job}",
            "manufacturer": "rustic-scheduler",
            "model": "Backup job",
            "sw_version": __version__,
        }
        for operation in Operation:
            base = f"{prefix}/jobs/{job}/{operation}"
            title = "Snapshot telemetry" if operation == Operation.SNAPSHOTS else str(operation)
            for key, component in (*_COMMON, *_EXTRA.get(operation, ())):
                object_id = f"{slug}_{operation}_{key}"
                topic = f"{discovery}/{component}/{ha.node_id}/{object_id}/config"
                wanted = operation in enabled and (key != "run" or operation.value in startable)
                if not wanted:
                    messages.append((topic, ""))
                    continue
                config = _entity(key, base, title.capitalize(), expiry) | {
                    "unique_id": f"{ha.node_id}_{object_id}",
                    "availability_topic": f"{prefix}/status",
                    "device": device,
                    "origin": origin,
                }
                messages.append((topic, json.dumps(config, separators=(",", ":"))))
    return messages
