# Restoring

A backup is only as good as its restore. Practise this before you need it.

The scheduler does not restore; it is done with Rustic, by you. Nothing about the repository
is scheduler-specific, so any machine with Rustic 0.11.4, the repository password and
credentials can restore, with or without this project.

> The local walkthrough below was run as written. The Glacier part was **not** run against
> AWS; see the note there.

## What you need

Keep these somewhere that does not depend on the machine being backed up:

- the repository password (the content of `rustic_password`): **without it the backup is
  unrecoverable**;
- the repository location: bucket, region, hot and cold prefixes, i.e. your repository
  profile;
- credentials allowed to read the bucket, and for cold storage to call `s3:RestoreObject`.

## 1. Find the snapshot

Inside the container, the helper supplies a job's profiles and environment:

```sh
docker compose exec rustic-scheduler rustic-scheduler rustic documents -- snapshots
docker compose exec rustic-scheduler rustic-scheduler rustic documents -- snapshots --filter-label documents
docker compose exec rustic-scheduler rustic-scheduler rustic documents -- ls latest
docker compose exec rustic-scheduler rustic-scheduler rustic documents -- ls 22342e20 /data/documents/taxes
```

`latest` is the newest snapshot of all; use an id, or add `--filter-label <job>`, when
several jobs share the repository. Listing snapshots and files reads metadata only, which in
a hot/cold repository comes from the hot part: no retrieval, no delay.

On any other machine, the same with plain Rustic:

```sh
export AWS_SHARED_CREDENTIALS_FILE=~/.aws/credentials
rustic -P /path/to/s3-deep-archive.toml snapshots
```

## 2. See what a restore would do

```sh
rustic -P /path/to/repo.toml restore --dry-run 22342e20 /restore/target
```

This prints how many files and bytes would be written, without reading file contents.

## 3. Restore from a repository whose data is readable

For a local repository, or S3 Standard / Glacier Instant Retrieval:

```sh
# everything in the snapshot
rustic -P /path/to/repo.toml restore 22342e20 /restore/target

# one folder of it
rustic -P /path/to/repo.toml restore 22342e20:/data/documents/taxes /restore/taxes
```

The container's source mounts are read-only and its root filesystem too, so restore to a
directory you mount for the purpose, or run Rustic on the host:

```sh
docker compose run --rm --user 0 -v /srv/restore:/restore --entrypoint rustic rustic-scheduler \
  -P /etc/rustic-scheduler/profiles/s3-deep-archive.toml restore latest /restore
```

`--user 0` lets Rustic recreate file ownership. Restoring never writes to the repository.

### Verify

```sh
diff -r /data/documents /restore/data/documents          # if the original still exists
rustic -P /path/to/repo.toml diff 22342e20 /restore/target
```

## 4. Restore from Glacier Flexible Retrieval or Deep Archive

> **Not tested against AWS.** The mechanism below is Rustic's documented warm-up feature. The
> only parts verified here are that Rustic runs the warm-up command once per needed pack and
> what it substitutes for `%id` and `%path` on an S3 backend. Whether the restore request,
> the wait and the subsequent download work on your account, and what they cost, is for you
> to test before you depend on it.

Archived data packs must be restored ("warmed up") before Rustic can download them. Rustic
does this in three steps during `restore`:

1. work out which packs the restore needs, from the index in the hot part;
2. run a **warm-up command** for each of them;
3. wait, then download.

Add to your repository profile (it is commented out in the example):

```toml
[repository]
warm-up-command = "aws s3api restore-object --bucket my-backup-bucket --key %path --restore-request Days=7,GlacierJobParameters={Tier=Bulk}"
warm-up-wait = "48h"
```

- `%path` is the object key of the pack, including the cold prefix, for example
  `cold/data/6a/6a40351a...`. `%id` is the pack id alone.
- `Tier=Bulk` is the cheapest and takes up to 48 hours on Deep Archive; `Standard` takes up
  to 12. `Days` is how long the restored copy stays readable, and billed at Standard rates.
- `warm-up-wait` makes Rustic sleep that long before downloading. Rustic also has
  `warm-up-wait-command`, which is run per pack and should return when the object is
  readable, to avoid waiting longer than needed.
- `aws` is the AWS CLI. It is not in the scheduler image. Run the restore from a machine that
  has it, or build an image with it.

Then:

```sh
# what is needed, and issue the restore requests, but restore nothing yet
rustic -P s3-deep-archive.toml restore --dry-run --dry-run-warmup latest /restore/target

# ... up to 48 hours later ...
rustic -P s3-deep-archive.toml restore latest /restore/target
```

Restore only what you need (`snapshot:/path`): cost and time follow the number of packs
involved, not the size of the repository.

### Delays and costs to expect

| Step | Time | Billed |
|---|---|---|
| Listing snapshots and files | seconds | a few requests on the hot part |
| Restore requests | minutes | per request |
| Waiting | up to 12h (Standard) or 48h (Bulk) on Deep Archive | retrieval per gigabyte |
| Download | by bandwidth | temporary Standard storage of the restored copies; transfer out of AWS per gigabyte |

See [aws-s3-glacier.md](aws-s3-glacier.md#minimum-storage-duration-retrieval-and-costs).

## 5. Test restores regularly

- **Monthly, cheap:** restore one small folder from the newest snapshot and compare it. On
  Deep Archive, choose a folder of a few megabytes and accept the wait.
- **After any change** to the repository profile, credentials or Rustic version.
- **Once, fully,** before trusting the setup: a complete restore of at least one job to a
  scratch location.
- The default `check` proves the metadata is consistent. It does not prove that data packs
  are intact or retrievable; only a restore or `check --read-data` does.

To practise the mechanics without AWS, use `examples/profiles/local-test.toml`: back up a
folder, delete a file, restore it. The integration tests do exactly this, including a restore
after forget and prune.

## If the scheduler's host is lost

1. Install Rustic 0.11.4 on any machine.
2. Recreate the repository profile (bucket, region, prefixes) and provide the password and
   AWS credentials.
3. `rustic -P repo.toml snapshots`, then restore as above.

No state from the scheduler is needed. Its state directory holds only last results and logs.
