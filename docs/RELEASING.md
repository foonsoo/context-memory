# Release policy

Context Memory has not published its first PyPI release yet. Until a release
tag and the trusted-publishing workflow complete, install from a pinned commit
or use the documented one-time source installation.

Release candidate `v0.6.2` completed the reproducible build, installed-wheel,
and TestPyPI installation jobs on 2026-08-25. Its production PyPI job was
skipped, so the remaining release gate is an explicit production-enabled
workflow run and environment approval followed by the PyPI `uvx` smoke test.

## Versioning and compatibility

The package uses semantic versioning. A release candidate must preserve the
documented compatibility baseline unless its changelog explicitly identifies a
breaking change. Database migrations are forward-only; release verification
must include backup/restore guidance and the installed-wheel restart test.
The normative runtime, MCP/CLI, migration, backup, deprecation, and recovery
guarantees are in [SUPPORT.md](SUPPORT.md).

## Release procedure

1. Start from a clean `main` that has passed CI. Update `CHANGELOG.md`, set the
   version in `pyproject.toml`, confirm that `SUPPORT.md` still matches the CI
   matrix and protocol declaration, and run the complete local verification
   matrix.
2. Build twice with the same `SOURCE_DATE_EPOCH` and verify that the wheel and
   source distribution hashes match. `scripts/verify_release.py` also verifies
   source/package versions, required package and migration files, wheel metadata,
   and the absence of unguarded runtime dependencies before tagging.
3. Create an annotated tag exactly matching the package version, such as
   `v0.6.0`, and push the tag.
4. Run the `Publish distributions` workflow for that tag. The workflow verifies
   the tag/version pair, builds once, records artifact provenance, publishes to
   TestPyPI, and installs that exact version from TestPyPI in a clean environment.
5. Only after the TestPyPI installation passes, approve the protected `pypi`
   environment when the workflow was started with production publishing enabled.
   PyPI receives the same downloaded workflow artifact, not a rebuild.
6. Create GitHub release notes from the changelog and verify `uvx --from
   context-memory-mcp context-memory --help` against PyPI.

The `testpypi` and `pypi` GitHub environments must each be configured for PyPI
trusted publishing. The `pypi` environment should require reviewer approval.
The workflow uses short-lived OIDC identity and stores no repository API token.

Never reuse or move a release tag, upload a locally rebuilt artifact, or publish
directly from an unreviewed branch. If TestPyPI validation fails, fix the issue,
bump the version, and begin again with a new tag.
