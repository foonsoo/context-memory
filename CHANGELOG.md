# Changelog

All notable changes to Context Memory will be documented here. The project
uses semantic versioning for the public Python package and MCP/CLI contracts.

## Unreleased

### Added

- A session-independent, estimated-token-bounded `context_recall` MCP tool for
  recovering the canonical project, repository artifacts, current decisions,
  and next actions from natural-language continuation requests.
- Frozen continuation, large-repository, and provenance-bearing retrospective
  replay evaluations covering Korean and English prompts, conservative project
  selection, source recovery, stale-content leakage, and traversal bounds.
- A seven-tool `minimal` profile, self-validating temporary-DB restart demo,
  contribution/security guidance, and macOS/release CI jobs.
- Synthetic alias regression data for no-answer, other-project, stale-decision,
  mixed-language, API, restart, and install prompts. It is not independent data.

### Changed

- Context recall can use bounded recent promotable events when active memories
  are unavailable, and prefers an event's explicit canonical repository path
  over a placeholder workspace alias.
- The installed-wheel release journey now verifies `context_recall` through the
  shipped MCP server after a process restart.
- Scope paths and path aliases share one transactional owner check, including
  import; same-named folders are not automatically joined.
- Active-memory creation and default import require a valid same-project source
  event. This proves traceability, not truth, and existing rows are unchanged.
- Import offers `--allow-legacy-active-without-sources` only for historical
  source-less active rows and reports count, IDs, and warning without bodies.
  Invalid, missing, and cross-project links remain errors.
- Task-specific global recall bridges (`install`→`wheel`, `client`→`handoff`,
  and package/move→`scope`) moved out of defaults; use project search aliases.
- `context_recall` remains free of persistent writes on verified paths.

### Compatibility

- Strict import may reject exports accepted by `0.6.2`. Back up the destination
  and use the legacy flag only for known historical source-less active rows;
  preferably attach confirming project-local events first.
- These changes are on `main`, not published `0.6.2`. The proposed next semantic
  version is `0.7.0`; no tag or upload has occurred.

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
