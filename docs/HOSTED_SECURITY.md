# Hosted identity and isolation contract

The hosted track is separate from the supported local-first product. The local
stdio runtime, SQLite file, and zero-runtime-dependency installation remain
unchanged. No existing bearer-token HTTP mode is promoted to an internet-facing
service by this design.

## Trust boundary

An edge authentication layer must verify a user or service identity and load
the current server-side session record before constructing `HostedSession`.
Opaque credentials, password material, and bearer tokens must never enter the
Context Memory database or authorization decision logs. A session marked
revoked, expired, or otherwise inactive is represented as inactive and is
rejected before tenant or role evaluation.

Authorization is deny-by-default and evaluated in this order:

1. require a verified, active, complete identity context;
2. require the resource tenant to equal the session tenant;
3. require an explicit role permission for the action;
4. for project operations, require the project in the session's server-loaded
   project grants;
5. for tenant backup, require the tenant-scoped backup role and reject a
   project-shaped resource.

Unknown roles grant no permissions. Tenant administrators remain tenant-bound;
they cannot cross a tenant boundary. Service identities use dedicated
least-privilege roles and do not inherit human administrator permissions.

## Initial permission matrix

| Role | Search | Event poll | Export | Tenant backup |
| --- | --- | --- | --- | --- |
| `project_reader` | granted projects | granted projects | no | no |
| `project_exporter` | no | no | granted projects | no |
| `tenant_backup_operator` | no | no | no | own tenant |
| `service_reader` | granted projects | granted projects | no | no |
| `tenant_admin` | granted projects | granted projects | granted projects | own tenant |

Project creation, grant administration, session issuance/revocation, account
deletion, and service-role assignment are intentionally not implied by this
matrix. They require separate privileged actions and audit contracts before any
remote endpoint is exposed.

## Persistence and request requirements

A hosted persistence schema must put `tenant_id` on every authoritative root
and enforce tenant-consistent foreign keys. Project and scope identifiers alone
are not authorization evidence. Repository methods must receive the authorized
tenant and project together; filtering results after a broad query is forbidden.
Session and grant changes must be checked server-side on every request or by a
short-lived cache with explicit revocation bounds.

Every denial returns a stable reason without revealing whether a foreign tenant
or project exists. Security audit records may contain actor, session, tenant,
action, decision, and request ID, but must exclude credentials and memory
content.

## Durable identity state

`HostedIdentityStore` persists tenant-scoped actors, projects, sessions, role
assignments, and project grants in a separate hosted control database. Composite
foreign keys prevent a grant or session from joining roots across tenants.
Request handling must load the session, its current roles, and its current
grants on every request. Expired or revoked sessions load as inactive, so
revocation takes effect on the next authorization check. Provisioning methods
are an internal persistence primitive, not a remote administration API.

## Gate before remote access

`HostedRepositoryGateway` is the mandatory boundary for protected content
operations. It authorizes before invoking storage, never calls storage after a
denial, and forwards the exact authorized tenant and project to a
`TenantConstrainedRepository`. A hosted adapter must implement that interface
with tenant predicates in the storage query itself; wrapping the local
single-user `MemoryStore` or filtering a broad result afterward is forbidden.

The policy kernel, durable identity state, and repository gateway are only
initial P7-4 slices. Remote access remains blocked until a concrete
tenant-keyed content store, privilege-administration rules, rate limits,
quotas, and end-to-end cross-tenant tests for search, export, event polling,
and backup are all implemented. Transport and API production controls remain
a separate P7-5 gate.
