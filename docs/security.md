# Security

## Secrets

| Secret | Where it lives | How it reaches the consumer |
|---|---|---|
| Repository password | mounted file | Rustic reads it: `password-file` in a profile |
| AWS access key and secret | mounted shared credentials file | OpenDAL reads it: `AWS_SHARED_CREDENTIALS_FILE` |
| MQTT password | mounted file | the scheduler reads it at each connection: `mqtt.password_file` |
| Apprise URLs and tokens | mounted Apprise config file | the scheduler reads it per notification: `notifications.config_files` |
| Pushgateway basic-auth password, if used | environment `RUSTIC_PROMETHEUS_PASS` | Rustic |

Rules the project follows:

- **No secret in the YAML configuration.** It has no field that accepts one: only paths.
- **No secret on a command line.** Command lines are visible in the process list, and Rustic
  stores the command line of each backup inside the snapshot. `extra_args` refuses
  `--password`, `--key` and `--prometheus-pass`. The scheduler itself only passes profile
  paths, labels, filters and source paths.
- **No secret in metrics.** Labels are job, operation and fixed vocabularies.
- **No secret in logs, run logs, MQTT payloads or notifications.** All of them pass through
  one masking step that hides: values registered at run time (the MQTT password, every
  Apprise URL), `scheme://user:password@` userinfo, `key=value` pairs whose key contains
  `password`, `secret`, `token`, `api_key` or `access_key`, and AWS access key ids. Masking
  is a safety net, not a guarantee: avoid debug log levels in Rustic profiles on shared
  systems.
- **`rustic show-config` output is never logged.** It can contain a password that a profile
  sets inline.
- **Examples contain placeholders only**, and `examples/secrets/*` is ignored by Git except
  for the `.example` files.

Prefer files over inline values in profiles: `password-file` instead of `password`, a shared
credentials file instead of `access_key_id` in `[repository.options]`. Rustic also supports
`password-command`, useful with a secret manager.

### File permissions

The container runs as UID 1000. Compose file-based secrets are bind mounts and keep the host
file's owner and mode, so make them readable by that user only:

```sh
chown 1000:1000 examples/secrets/rustic_password && chmod 600 examples/secrets/rustic_password
```

The scheduler process itself has no special privileges, so a secret file that UID 1000
cannot read (the MQTT password, the Apprise file) fails with a permission error.

Profiles and the YAML are mounted read-only and may be world-readable, since they hold no
secrets.

### Rotation

| Secret | To rotate |
|---|---|
| AWS keys | Replace the credentials file. Each Rustic process reads it when it starts. |
| MQTT password | Replace the file. Used at the next reconnect; restart the container to force one. |
| Apprise targets | Replace the file. Used for the next notification. |
| Repository password | `rustic key add` with the new password, update the file, then `rustic key remove` the old key. The repository's data is not re-encrypted. |

### Environment variables

Rustic inherits the scheduler's environment plus `rustic.env` and the job's `env`. Anything
there is visible to Rustic, to its hook commands and to anyone who can read
`/proc/<pid>/environ` as the same user or inspect the container. Use environment variables
for paths and switches, not for secret values. The one unavoidable exception is
`RUSTIC_PROMETHEUS_PASS` for an authenticated Pushgateway, since Rustic offers no file
option; prefer keeping the Pushgateway on a private network.

## Container hardening

The Compose example runs the scheduler:

- as **UID 1000**, not root;
- with a **read-only root filesystem** and a tmpfs `/tmp`;
- with **all capabilities dropped** except one in the bounding set (below);
- with **source data mounted read-only**;
- with writable storage limited to two volumes: state and Rustic's cache;
- with the metrics port published on loopback only, and the Pushgateway not published.

### Reading files the user cannot read

A backup must read every file, including files only root can read. Instead of running as
root, the image carries a second copy of the Rustic binary with the file capability
`CAP_DAC_READ_SEARCH`, and the Compose file adds that capability to the container's bounding
set:

```yaml
cap_drop: [ALL]
cap_add: [DAC_READ_SEARCH]
```

- Only the Rustic process gains the capability, when it is executed. The scheduler, its HTTP
  server, MQTT and notification code run with no capability at all.
- `CAP_DAC_READ_SEARCH` bypasses read and directory-search permission checks. It does not
  allow writing, changing ownership, or anything else root can do. Combined with read-only
  mounts, Rustic can read everything and modify nothing.
- Hook commands configured in a profile are started by Rustic and do **not** inherit it.
- The scheduler picks that binary only when the capability is available (it checks its
  bounding set), because executing it without would fail. Otherwise it uses plain `rustic`
  and files UID 1000 cannot read make the backup fail visibly.
- **Do not set `no-new-privileges`** on this container: it disables file capabilities and
  the privileged copy would run without its capability.

Alternatives: remove `cap_add` and set `user:` to the owner of your data, for no capability
at all; or `user: "0:0"` to run as root, which the read-only mounts still constrain.

The capability lets Rustic read every file in the container's mounts, secrets included. Mount
only what should be backed up.

### Network

| Port | Exposes | Protect by |
|---|---|---|
| 9300 | read-only metrics, health, status JSON | binding to loopback or a private network. Status includes job names, source paths in error text and timings. There is no authentication. |
| Pushgateway 9091 | read **and write** of pushed metrics | not publishing it; see [metrics.md](metrics.md#securing-it) |
| Control socket | manual triggers | file mode 0600 in the state volume; not reachable over the network |

Nothing reachable over TCP can start, stop or alter a backup.

**TLS.** Rustic talks to S3 over HTTPS by default (your `endpoint`, if set, decides). MQTT
supports TLS with certificate verification (`mqtt.tls`). The metrics endpoint is plain HTTP;
put a reverse proxy in front of it if it must cross an untrusted network.

## Repository safety

- The scheduler never initialises, migrates or repairs a repository.
- Nothing destructive is enabled by default, and destructive operations cannot be requested
  manually unless enabled; see [maintenance.md](maintenance.md#safeguards).
- Consider credentials without `s3:DeleteObject` for the scheduler, so that a compromised
  backup host cannot delete backups; see [aws-s3-glacier.md](aws-s3-glacier.md#permissions).
- The repository password protects confidentiality. Anyone with write access to the bucket
  can still delete or damage data: use bucket policies, versioning or Object Lock for that.

## Supply chain

The image build pins the base image and the uv image by digest, Rustic by version and
SHA-256 per architecture, and Python packages by `uv.lock` with hashes. The Pushgateway image
in Compose is pinned by digest. `scripts/install-rustic.sh` verifies the same checksums for
development.
