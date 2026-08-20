# Authorized source client workflow

Context Memory does not log into Confluence-like systems, hold vendor credentials, crawl pages, or store full page bodies. An authorized client owns retrieval and passes only stable source metadata plus decision-relevant claims to the generic research provenance tools.

The checked example in [`examples/confluence-like-source-workflow.json`](../examples/confluence-like-source-workflow.json) covers two flows:

1. **Initial analysis:** the client reads a page with the user's existing access, creates an investigation, and records one immutable page version with typed evidence and a proposed decision.
2. **Reinspection:** the client records why reinspection is needed, follows the returned canonical URI using its own authorization, and records a changed page as a new source analysis. The earlier version remains immutable.

## Client responsibilities

- Confirm authorization before reading the external page.
- Use a vendor-stable page identifier and version when available; otherwise use the privacy-safe analyzed-content fingerprint supported by `investigation_record_source`.
- Extract only claims that materially affected the investigation. Do not send access tokens, cookies, full pages, attachment bodies, or general browsing history.
- Separate evidence from inference. Inference stays proposed, and action or decision claims cite the exact evidence claims that informed them.
- Reuse the original `investigation_id` when a later page version answers the same research question.
- Use idempotency keys so retries do not duplicate investigations, analyses, or reinspection requests.

## Core responsibilities

- Preserve source identity/version metadata, typed claims, immutable events, cited memories, and causal links.
- Return the canonical URI and inspected version for a reinspection request without fetching it.
- Distinguish a newer source version from the earlier analysis and never rewrite historical evidence.
- Keep authorization, retrieval, and vendor APIs outside the core.

The JSON example uses symbolic `$PROJECT_ID`, `$SCOPE_ID`, `$investigation_id`, and `$source_analysis_id` values. A client replaces them with values captured from prior calls. CI validates every illustrated MCP call against the shipped tool schemas.
