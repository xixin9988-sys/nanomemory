# Backup, verify and restore (vector database)

The vector database is a single SQLite file, but copying it while the service is writing is not a
backup: committed pages may still live in the `-wal` file.  Use the online backup API exposed by
`nanomemory/vector/backup.py` (and the CLI wrapper).

## 1. Create

~~~bash
python scripts/vector_backup.py backup \
  --database storage/vector/vectors.db \
  --out backups/
~~~

Output: `backups/vectors-<timestamp>-<id>.db` plus `.json` manifest containing SHA-256, size,
schema version and row counts. The command runs `PRAGMA integrity_check` before writing the
manifest.

## 2. Verify

~~~bash
python scripts/vector_backup.py verify --backup backups/vectors-<...>.db
~~~

Exit code 0 only when: manifest hash matches, integrity_check is `ok`, and the backup schema is
not newer than this build. Any corruption (truncation, bit flip) fails verification.

## 3. Restore

~~~bash
# stop the service first: SQLite cannot replace a file a writer has open
python scripts/vector_backup.py restore \
  --backup backups/vectors-<...>.db \
  --database storage/vector/vectors.db \
  --force
~~~

What the command does, in order:

1. re-verifies the backup;
2. snapshots the current database to `vectors.pre-restore-<timestamp>.db` when `--force` is used;
3. restores into a temp file, runs `integrity_check`, then atomically replaces the target;
4. removes stale `-wal`/`-shm` files.

If anything fails before the atomic replace, the current database is untouched.

## 4. Drill checklist (quarterly or before a paid pilot)

- [ ] Backup created from a database with active writes.
- [ ] `verify` returns `valid: true`.
- [ ] Restore into a **different directory** first; start the API against it; run `/health`.
- [ ] Compare memory counts by state and a sample of `GET /api/v1/memory/vectors`.
- [ ] Run `pytest tests/contract -q` against the restored copy (point storage root at it).
- [ ] Record duration and file size in the operations log.
- [ ] Delete the drill copy and the pre-restore snapshot per retention policy.

## 5. Failure modes

| Symptom | Cause | Action |
| --- | --- | --- |
| `database is locked` | service still running during restore | stop the service, retry |
| `backup is not restorable: sha256` | file modified after backup | use another backup; never bypass verification |
| `schema N is newer` | backup from a newer build | upgrade the binary before restoring |
| `database disk image is malformed` | truncated/corrupt copy | restore from the previous verified backup |
| Missing recent writes after restore | backup taken after a crash before WAL checkpoint | WAL is included by the backup API; if not, the backup was a file copy — fix the procedure |

## 6. What this does not cover

- Business database (Postgres) backups — handled by the application's own tooling.
- Off-site replication and encryption at rest — operator responsibility.
- Point-in-time recovery: SQLite gives you the last consistent backup, not an arbitrary timestamp.
