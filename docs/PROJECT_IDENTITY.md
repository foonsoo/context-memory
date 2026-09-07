# Project identity and provenance migration

Automatic ownership uses only an exact canonical scope path or an explicitly
registered `path` alias. Folder names and Git remotes may help discovery, but do
not choose a write target. Register a moved checkout explicitly with
`set_project_alias(project_id, "path", new_path)` through the Python API; a
future CLI/MCP write surface may make this easier. A path already owned by a
different project is rejected.

Use `project_alias_list` (or `project-list` plus export) to inspect every path
attached to a project. If an older version merged two same-named checkouts, make
a backup, export the affected project, inspect source event IDs and scopes, then
create the intended projects and import or recreate only records whose ownership
you can establish. The program deliberately does not auto-split old data.

New `active` memories must cite at least one existing event in the same project.
First call `record_event`, then pass its ID to `memory_upsert.source_event_ids`.
Existing valid links count during later activation. `doctor` reports the count
and IDs of legacy active memories without sources without printing their bodies.
Back up the database, record a confirming event for each memory whose content you
can verify, attach it by upserting the same memory ID as `proposed`, and then
activate it. Records that cannot be verified may remain legacy data or be moved
to a non-active lifecycle state; no automatic deletion or downgrade occurs.

Imports preserve existing legacy records for compatibility. Any new activation
after import follows the source requirement. A source link establishes an audit
trail, not the truth of its contents.
