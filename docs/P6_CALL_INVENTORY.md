# P6 `MemoryStore` call inventory

This inventory is the evidence gate for P6 dead-code removal. Generate the
machine-readable form with:

```bash
python benchmarks/call_inventory.py
```

The 2026-08-21 baseline contains 89 methods: 61 public methods (including the
constructor) and 28 private methods. Syntax-tree call sites are classified as
`store_internal`, `mcp`, `cli`, `hooks`, `tasks`, `tests`, or
`other_python`. The compatibility snapshot separately freezes MCP and CLI
contracts, record types, deterministic retrieval results, Wiki Markdown, and
import/export behavior.

## Findings

- Every private method has at least one static call site. No private production
  method is currently proven unreachable, so this slice removes none.
- All 60 non-constructor public methods remain compatibility surface even when a
  method is used only internally today. Public naming plus the documented
  `context_memory.store.MemoryStore` import makes absence of a repository call
  site insufficient removal evidence.
- MCP dispatch directly maps its tools to `MemoryStore` bound methods. CLI,
  hooks, and Tasks add independent call paths; tests exercise all public methods
  and three private retrieval helpers.
- Vulture 2.16 at 80% confidence reports no unused production symbol. Its only
  repository finding is the `tz` argument in the test replacement for
  `datetime.now`; that argument is required for signature compatibility and is
  not dead code.

## Removal rule

A later change may remove a private method only when this inventory reports no
call site and characterization coverage proves the path unreachable. Public
methods require an explicit documented deprecation path in addition to the
compatibility baseline. Text search alone is not removal evidence.
