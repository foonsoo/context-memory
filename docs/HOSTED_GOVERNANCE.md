# Hosted data governance and privacy operations

This policy applies only to a future hosted service. The local-first product
continues to store its database under the user's control. A hosted deployment
must publish its configured values before accepting user data; the defaults in
code are an executable contract, not a substitute for a public privacy notice.

## Purpose and data lifecycle

The hosted service collects tenant, actor, project, session, authorization,
security-audit, and user-selected memory-event data to preserve decision
evidence, retrieve project context, enforce access, and operate the service.
Operators must not reuse event content for an unrelated purpose without an
explicit policy change and appropriate user notice.

`HostedGovernancePolicy` requires a collection purpose and exposes the event
retention period, backup retention period, storage region and class, incident
contact, and runbook. The code defaults are 365 days for events and 30 days for
backups. A deployment may choose shorter periods and must disclose its actual
values. Event retention runs per tenant and deletes events older than the
computed cutoff; it does not inspect or affect another tenant.

Storage region and storage class are operator-selected deployment choices.
Production configuration must name the real region and an encrypted storage
class, document any cross-region replication, and keep identity, content,
governance journals, and backups within the disclosed boundary.

## Export and erasure

Authorized privacy workflows can export one actor's identity record or one
project's events. Exports are always keyed by tenant plus actor/project; callers
must authenticate and authorize the request before invoking this internal
service.

Erasure supports three scopes:

- actor erasure removes the actor, sessions, roles, grants, and matching
  security-audit identifiers, but preserves shared project content;
- project erasure removes project events, grants, identity metadata, and
  matching audit identifiers;
- tenant erasure removes all content, identity state, pending backup registry
  entries, and the tenant roots.

Content and identity are separate databases, so erasure cannot be one SQLite
transaction. A durable journal records completion of each idempotent stage and
allows retry after interruption. On completion the raw journal row is deleted;
the receipt retains only request, idempotency, and subject hashes. A production
worker must retry pending rows and alert when they exceed its completion
threshold.

Backup objects are deleted by the configured storage provider, not by the
SQLite registry. The registry reports expired objects and records deletion only
after the provider confirms success. Tenant erasure pauses with the required
provider object keys and cannot complete or remove registry rows until those
deletions are confirmed. Provider lifecycle rules must enforce
the same expiry independently, and operators must periodically compare provider
inventory with this registry.

## Sensitive data

The best-effort scanner can warn about private-key markers, credential-like
assignments, email addresses, and Korean resident-number-shaped text. Warnings
contain only category, code, and count; they never echo the match. Detection is
non-blocking and every result states that detection is incomplete. It is a user
warning and policy signal, not a claim that secrets or personal information can
be found reliably. Access controls, encryption, minimization, retention, and
erasure remain mandatory even when no warning is produced.

## Incident response

The configured incident contact owns the following runbook:

1. contain the affected credential, tenant, route, or storage system without
   destroying evidence needed for investigation;
2. preserve redacted request IDs, security-audit records, timelines, and the
   affected policy configuration, never raw secrets in tickets or logs;
3. determine affected tenants, data classes, regions, and time range;
4. restore from a verified backup or resume an erasure journal when recovery is
   needed, and validate tenant isolation before reopening traffic;
5. notify users and authorities according to the deployment's contractual and
   legal obligations, then document corrective actions and regression tests.

P7-7 defines operational alerts and recovery drills. P7-8 must prove interruption,
restore, concurrency, and capacity behavior before the hosted listener can be
deployed.
