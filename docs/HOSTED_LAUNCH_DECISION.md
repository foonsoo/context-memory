# Hosted launch decision

**Decision date:** 2026-09-01  
**Status:** decided — defer launch  
**Decision:** Do not launch or expose a hosted Context Memory service now.
Keep the published local-first package as the supported product and retain the
hosted implementation as an undeployed, tested prototype.

## Why

P7 proved that the repository can enforce the required identity, isolation,
transport, governance, operations, load, and recovery contracts. That is an
implementation-readiness result, not evidence that operating a remote service
is the right product decision.

The available adoption signals do not justify assuming hosted demand:

- the public repository reported 0 stars, 0 forks, and 0 open issues at the
  decision snapshot;
- PyPI Stats reported 116 downloads in the preceding month, but package
  downloads include CI, release verification, upgrades, and repeated installs
  and therefore do not establish independent users or multi-machine demand;
- no verified request for shared multi-user or multi-machine hosting is recorded
  in the project evidence.

A launch would add continuous security response, privacy and deletion
operations, incident ownership, backup custody, availability commitments, and
recurring infrastructure cost. Selecting a cloud, identity provider, region,
or storage topology before demand is verified would create commitments without
evidence that they solve a user problem.

## Consequences

- Do not deploy the hosted listener, add it to local CLI onboarding, provision
  cloud resources, select an identity provider, or publish an external SLO.
- Continue supporting the local stdio/localhost product and its portable
  export/import and backup/restore paths.
- Keep hosted tests and documentation in the repository so the implementation
  evidence remains reproducible. Hosted changes must not weaken the local
  zero-runtime-dependency path.
- Do not cut a release solely to advertise the hosted prototype. A later normal
  package release may include the modules as non-public implementation detail
  if its release scope independently warrants that baseline.

## Reconsideration gate

Reopen the launch decision only after recording all of the following:

1. verified requests from at least three independent users or teams whose
   workflow requires continuous multi-machine or multi-user access and cannot
   be served adequately by local export/import;
2. a named product owner and incident operator, with an approved operating
   budget and supported regions;
3. an explicit data-controller/privacy position covering retention, deletion,
   subprocessors, backup custody, and security-response ownership;
4. representative workload data sufficient to choose an identity provider,
   trusted edge, runtime, SQLite storage class, backup service, concurrency
   target, and SLO without guessing.

When the gate is met, repeat the environment-specific isolation, privacy, load,
failure, migration, and restore validation in the selected topology before any
internet exposure.

## Evidence snapshot

- GitHub repository metadata:
  <https://api.github.com/repos/foonsoo/context-memory>
- PyPI Stats recent downloads:
  <https://pypistats.org/api/packages/context-memory-mcp/recent>
- Published package metadata:
  <https://pypi.org/pypi/context-memory-mcp/json>
- P7 implementation and deployment boundary: `docs/ROADMAP.md`,
  `docs/HOSTED_SECURITY.md`, `docs/HOSTED_TRANSPORT.md`,
  `docs/HOSTED_GOVERNANCE.md`, and `docs/HOSTED_OPERATIONS.md`
