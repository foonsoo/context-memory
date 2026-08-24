# Changelog

All notable changes to Context Memory will be documented here. The project
uses semantic versioning for the public Python package and MCP/CLI contracts.

## Unreleased

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

### Changed

- Persistence, CLI, and MCP declarations were decomposed behind stable public
  contracts, with all production modules covered by the repository Ruff rules.

### Security

- Context Memory remains local-first. The default database is not encrypted;
  users should not record secrets or credentials.

[Unreleased]: https://github.com/foonsoo/context-memory/compare/v0.6.0...HEAD
