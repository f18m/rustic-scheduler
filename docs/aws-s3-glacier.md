# AWS S3 and Glacier Deep Archive

> **Unverified against AWS.** No part of this project was run against an AWS account. What
> was tested, with a local S3-compatible server, is listed at the end. Everything about AWS
> storage classes, retrieval, billing and permissions below comes from AWS's and Rustic's
> documentation. Prices are indications; check the
> [S3 pricing page](https://aws.amazon.com/s3/pricing/) for your region.

## Which "Glacier"

Two different things carry the name:

- **S3 Glacier storage classes**: `GLACIER_IR` (Instant Retrieval), `GLACIER` (Flexible
  Retrieval) and `DEEP_ARCHIVE`. They are properties of ordinary S3 objects, in ordinary
  buckets, used through the ordinary S3 API. **This is what Rustic and this project use.**
- **The original Amazon Glacier service**: a separate, older API built on vaults and
  archives (`glacier:*` permissions, `aws glacier` commands). Rustic does not speak it, and
  nothing here applies to it. AWS steers new use to the S3 storage classes.

## Hot and cold repositories

Objects in `GLACIER` and `DEEP_ARCHIVE` cannot be read until they are restored, which takes
hours. A backup tool that had to read its own metadata from there would be unusable, so
Rustic splits a repository in two:

| Part | Rustic setting | Contains | Storage class |
|---|---|---|---|
| cold | `repository` | Everything: config, keys, snapshots, index, tree packs, **data packs** | Deep Archive |
| hot | `repo-hot` | A copy of everything except data packs: config, keys, snapshots, index, tree packs | Standard |

Backup, snapshot listing, forget and the default check only need the hot part. File contents
are written to the cold part and never read back until a restore, a `check --read-data`, or a
repack.

### Profile

[examples/profiles/s3-deep-archive.toml](../examples/profiles/s3-deep-archive.toml):

```toml
[repository]
repository = "opendal:s3"
repo-hot = "opendal:s3"
password-file = "/run/secrets/rustic_password"
cache-dir = "/var/cache/rustic"

[repository.options]          # shared by both parts
bucket = "my-backup-bucket"
region = "eu-west-1"

[repository.options-hot]
root = "/hot"

[repository.options-cold]
root = "/cold"
default_storage_class = "DEEP_ARCHIVE"
```

- One bucket with two prefixes, as here, or two buckets (`bucket` under `options-hot` and
  `options-cold`).
- `default_storage_class` is an OpenDAL S3 option: each object is uploaded directly into that
  class. No lifecycle rule is needed, and none should move the hot prefix to a cold class.
- The option names under `[repository.options*]` are OpenDAL's:
  <https://opendal.apache.org/docs/rust/opendal/services/struct.S3.html>.
- Do not enable S3 Intelligent-Tiering archive tiers or lifecycle transitions on the hot
  prefix; Rustic must be able to read it at any time.

### An existing repository

The scheduler works with an existing hot/cold repository as it is: point a profile at it.
It never initialises, migrates or converts a repository. `backup` only adds objects.
Nothing is deleted unless you enable `forget` or `prune`.

To create a new one, run `rustic init` yourself once (see the README).

## Credentials

Rustic 0.11.4 reaches S3 through OpenDAL 0.58. Preferred setup: a **shared credentials file**
mounted as a secret, named by `AWS_SHARED_CREDENTIALS_FILE`.

```yaml
rustic:
  env:
    AWS_SHARED_CREDENTIALS_FILE: /run/secrets/aws_credentials
    AWS_CONFIG_FILE: /dev/null
    AWS_EC2_METADATA_DISABLED: "true"
```

```ini
# /run/secrets/aws_credentials
[default]
aws_access_key_id = ...
aws_secret_access_key = ...
```

Verified with the pinned Rustic against a local S3 server: with only this file, Rustic
authenticates; with the variable pointing at an empty file, it fails with a signing error.
Rotating the keys means replacing the file; the next Rustic process reads the new one.

Other sources OpenDAL documents, and their order of precedence (not verified here beyond the
first two):

1. `access_key_id` / `secret_access_key` in `[repository.options]`. Works, but puts secrets in
   a TOML file and in `rustic show-config` output. Avoid.
2. Environment `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`. Visible to
   anything that can read the process environment.
3. Shared files: `AWS_SHARED_CREDENTIALS_FILE` (default `~/.aws/credentials`) and
   `AWS_CONFIG_FILE` (default `~/.aws/config`), profile chosen with `AWS_PROFILE`.
4. Web identity (`AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_ROLE_ARN`), ECS container credentials,
   EC2 instance metadata.

Limitations to keep in mind: `credential_process` and SSO profiles in `~/.aws/config` are not
documented as supported by OpenDAL's loader; do not rely on them without testing. On a
machine that is not EC2, disable the metadata lookup as above to avoid a slow timeout.

## Permissions

A starting point for an IAM policy, to be tightened and tested by you. Replace the bucket.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "List",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
      "Resource": "arn:aws:s3:::my-backup-bucket"
    },
    {
      "Sid": "BackupAndRead",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": "arn:aws:s3:::my-backup-bucket/*"
    },
    {
      "Sid": "ForgetAndPrune",
      "Effect": "Allow",
      "Action": ["s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-backup-bucket/*"
    },
    {
      "Sid": "RestoreFromArchive",
      "Effect": "Allow",
      "Action": ["s3:RestoreObject"],
      "Resource": "arn:aws:s3:::my-backup-bucket/cold/*"
    }
  ]
}
```

| Activity | Needs |
|---|---|
| backup, snapshots, default check | List, BackupAndRead |
| forget, prune | plus ForgetAndPrune |
| restore, `check --read-data` | plus RestoreFromArchive (for the warm-up command) |

A backup never deletes anything. Leaving `s3:DeleteObject` out of the scheduler's credentials
gives a backup-only identity that cannot destroy existing backups, which limits what stolen
credentials or ransomware on the backup host can do. Forget and prune then have to be run
with a different identity. Bucket versioning or Object Lock add protection against
overwrites; both change what deletions cost.

The warm-up command in the example profile uses the AWS CLI, which is not in the image; see
[restore.md](restore.md).

## Minimum storage duration, retrieval and costs

Figures as published by AWS for `DEEP_ARCHIVE` at the time of writing; verify them.

| Property | Deep Archive |
|---|---|
| Minimum storage duration | **180 days**. Deleting or overwriting earlier is billed for the remaining days. |
| Retrieval time | Standard: within 12 hours. Bulk: within 48 hours. No expedited tier. |
| Per-object overhead | About 40 KB billed per object (32 KB at the archive rate, 8 KB at the Standard rate) |
| Storage price | Roughly a twentieth of S3 Standard |
| Retrieval | Billed per gigabyte and per request; Bulk is the cheapest |
| Restored copy | Stored temporarily at the Standard rate for the days you request |
| Transfer out of AWS | Billed per gigabyte when restoring to a machine outside AWS |

For comparison, `GLACIER` (Flexible Retrieval) has a 90-day minimum and retrievals in minutes
to hours; `GLACIER_IR` has a 90-day minimum and immediate access at a higher storage price,
and needs no restore step at all.

What this means for a Rustic repository:

- **Backups are cheap**: uploads go straight to the archive class; nothing is read back.
  Rustic packs data into files of tens of megabytes, so the per-object overhead is small.
- **The hot part costs Standard rates**, but holds only metadata: usually a few percent of
  the repository.
- **Restores are slow and billed**: plan for up to 48 hours before the first byte, and for
  retrieval plus transfer charges proportional to what you restore.
- **`check --read-data` is a full restore** in cost terms. Use a subset, rarely.

### Deleting data: forget and prune

- `forget` deletes snapshot files, a few kilobytes each, from both parts. An early-deletion
  charge on the cold copy is negligible.
- `prune` deletes data packs. Each pack deleted before 180 days is billed as if it had
  stayed. Pass `--keep-pack 180d` so prune leaves younger packs alone:

  ```yaml
  prune:
    enabled: true
    schedule: {days: [sun], times: ["04:00"]}
    extra_args: ["--keep-pack", "180d"]
  ```

- Repacking a partly used data pack requires reading it, hence restoring it. Rustic's default
  for hot/cold repositories is to repack only cacheable (tree) packs, which live in the hot
  part. Leave that default.
- A retention policy shorter than 180 days saves little on Deep Archive: the data is paid for
  180 days either way.

## What was actually tested

With the pinned Rustic and a local S3-compatible server (Versity S3 Gateway), in
`tests/integration/test_services.py`:

- a hot/cold repository on S3 with `opendal:s3`, driven by the scheduler: backup, snapshot
  telemetry and the default check succeed;
- the hot prefix receives config, keys, snapshots, index and tree packs; the cold prefix
  receives all of those plus the data packs;
- credentials from `AWS_SHARED_CREDENTIALS_FILE` alone work; without them the backup fails
  cleanly and the secret key appears in no log or result;
- with `default_storage_class = "DEEP_ARCHIVE"` under `options-cold`, Rustic sends
  `x-amz-storage-class: DEEP_ARCHIVE` on every upload to the cold prefix and on none to the
  hot prefix;
- a restore from that repository returns the original file, and Rustic passes the pack's
  object key (for example `cold/data/6a/6a40...`) to the warm-up command as `%path`.

**Not tested, on any AWS account:** real storage-class transitions and billing; that objects
in `DEEP_ARCHIVE` are unreadable until restored; `RestoreObject` and the warm-up workflow;
retrieval times; early-deletion charges; the IAM policy above; credential sources other than
static keys in a shared credentials file; prune on archived data.
