# Support and compatibility policy

This policy describes the local-first Python package. A hosted or broadly
distributed service is a separate product track and is not supported by the
current release line.

## Supported runtime

- **Python:** CPython 3.11, 3.12, 3.13, and 3.14. CI runs the complete
  standard-library suite on every listed version. A Python version reaches end
  of support here no earlier than its upstream end of life and only in a
  release that documents the change.
- **SQLite:** the `sqlite3` library bundled with the selected CPython must
  provide WAL mode and FTS5. Context Memory does not replace the interpreter's
  SQLite library or promise compatibility with a numeric SQLite version in
  isolation. Run `context-memory doctor`; an unavailable FTS5 build is an
  unsupported runtime and fails explicitly.
- **Operating systems:** Linux is the CI release gate. macOS is routinely used
  for development and release-candidate verification. Windows is best-effort
  until it has a dedicated CI job; platform-specific client registration and
  file-permission behavior are not yet a release guarantee there.
- **Deployment boundary:** one local OS-user trust boundary, local filesystems,
  and one SQLite database shared by local MCP processes are supported. Live WAL
  databases on network or file-synchronization folders, cross-OS-account
  sharing, and production multi-user HTTP deployment are unsupported.

The default package has no runtime Python dependencies. Optional `crypto` and
`neural` extras follow their declared dependency ranges but do not expand the
supported deployment boundary.

## MCP and command compatibility

The server currently declares MCP protocol `2025-06-18`. Release verification
uses the official Python MCP SDK 2.0.0 with multiple stdio clients. Local stdio
is the primary supported transport; the localhost HTTP transport remains a
local integration surface, not a production network service.

The versioned compatibility fixture freezes MCP tool names, input schemas and
annotations; CLI commands and arguments; exported record types; compact context
and Decision Brief structures; Wiki rendering; and retrieval constants. A patch
release must preserve those public contracts. Before 1.0, an intentional
breaking public-contract change requires a minor-version bump and an explicit
changelog migration note.

Deprecated public behavior remains available through at least the next minor
release when safe and practical. Removal must be announced in the changelog.
Security, corruption, or privacy risks may require faster removal; the release
notes must identify the reason and recovery path.

## Database and migration guarantees

Schema migrations are ordered, bundled with the wheel, forward-only, and
recorded in `schema_migrations` only after their SQL transaction succeeds.
Opening an older database with a newer release applies every missing migration.
Downgrading a migrated database to an older binary is not supported.

Before an upgrade that may migrate the database:

1. Finish in-flight writes and create an integrity-checked `context-memory
   backup` snapshot.
2. Install the new package and run `context-memory doctor` against the database.
3. Restart long-running MCP clients so they load the new code and tool catalog.
4. Retain the pre-upgrade snapshot until the new version has completed a normal
   task lifecycle and a fresh backup.

Migrations preserve immutable events and authoritative memory data unless a
release explicitly documents a breaking migration. A migration failure must
leave its version unapplied; recovery is restore-forward, not an in-place schema
downgrade.

## Backup, restore, and portable data

`context-memory backup` uses SQLite's Online Backup API, includes committed WAL
data, verifies integrity, and writes one portable SQLite snapshot. A snapshot is
supported for restore with the same release or a newer release that can migrate
it. Optional encrypted envelopes additionally require the original passphrase
and a compatible `crypto` extra.

Project JSONL export/import is the supported logical transfer path across
machines. Import is additive and does not overwrite an existing project
identity; FTS projections are rebuilt from authoritative memory rows. Keep both
a database snapshot and a project export when long-term recovery is important.

Never restore by copying only the main file of a live WAL database. Never assume
that a newer migrated database can be opened safely by an older package.
Use `restore-db` after stopping connected clients. Restoring to a new path checks
the source before installing it. Replacing the authoritative path additionally
requires an integrity-checked backup of the current database and exact resolved-
path confirmation. Encrypted envelopes must be authenticated with
`backup-decrypt` before restore.

Complete local erasure requires an integrity-checked backup and exact resolved
database-path confirmation through `erase-db`. Stop connected clients first.
The command removes the authoritative database and its WAL sidecars, but client
registration cleanup and package removal remain separate operations.

## Failed-upgrade recovery

If `doctor`, startup, or a normal lifecycle check fails after an upgrade:

1. Stop clients that point at the affected database and preserve the failed
   database plus its `-wal` and `-shm` files for diagnosis.
2. Do not retry with an older binary against the migrated database.
3. Restore the pre-upgrade snapshot to a different local path, run `doctor`
   using the previously working package, and verify a read-only context query.
4. Re-register clients to the verified restore path if service must resume.
5. Report the package version, Python and SQLite versions, migration state, and
   redacted error. Do not attach a database containing private memory unless it
   has been intentionally sanitized.

The project does not yet promise a fixed release cadence or paid support SLA.
Compatibility bugs and security reports should include the smallest synthetic
reproduction that preserves user privacy.
