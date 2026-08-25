# Changelog

All notable changes to Context Memory will be documented here. The project
uses semantic versioning for the public Python package and MCP/CLI contracts.

## Unreleased

## [0.6.2] - 2026-08-25

### Added

- Decision Brief retrieval, research provenance, topic Wiki revisions,
  deterministic review/lint, source reinspection, Wiki navigation/export, and
  client-neutral checkpoint workflows.
- A compatibility baseline covering public MCP, CLI, persistence, rendering,
  and retrieval contracts.
- An explicit trusted-publishing workflow that verifies a release tag, tests
  the built distributions on TestPyPI, and can then publish the same artifacts
  to PyPI with build provenance.
- A support and compatibility policy covering runtimes, MCP/CLI contracts,
  migrations, backups, deprecations, and failed-upgrade recovery.
- A shared release verifier used by CI and publishing to enforce version,
  archive-content, zero-runtime-dependency, and reproducible-build contracts.
- Release builds write both distribution sets outside the source tree so the
  second sdist cannot recursively capture the first build output.
- The published distribution is named `context-memory-mcp` because PyPI's
  protected-name rules reject `context-memory`; the `context-memory` command
  and `context_memory` import package remain unchanged.

### Changed

- Persistence, CLI, and MCP declarations were decomposed behind stable public
  contracts, with all production modules covered by the repository Ruff rules.

### Security

- Context Memory remains local-first. The default database is not encrypted;
  users should not record secrets or credentials.

[Unreleased]: https://github.com/foonsoo/context-memory/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/foonsoo/context-memory/releases/tag/v0.6.2
