# Hosted transport and API contract

The existing stdio server and localhost HTTP integration are not the hosted
service transport. They retain their current local-first support boundary.
Any future hosted adapter must compose the contracts in
`context_memory.hosted_transport` before dispatching to the P7-4 identity and
repository gateways. Merely binding the local bearer-token server to a public
interface is unsupported.

`HostedAPIAdapter` is the concrete pre-authenticated boundary. An edge supplies
the server-loaded `HostedSession` plus peer, TLS, forwarded-header, origin,
version, body-length, request-ID, and idempotency metadata. The adapter applies
transport policy before invoking `HostedRepositoryGateway` or
`HostedAdministrationGateway`, converts opaque event cursors at the boundary,
and emits stable response envelopes without exposing authorization reasons.

## Deployment trust boundary

TLS terminates at the service process or a specifically configured reverse
proxy. Forwarded client and scheme headers are accepted only when the immediate
peer belongs to an exact configured trusted-proxy CIDR. Untrusted forwarded
headers are ignored and cannot assert HTTPS. The adapter rejects a resolved
non-HTTPS request before authentication or content dispatch.

Network-level limits for requests without a verified tenant and actor belong at
the edge. After identity verification, the persistent P7-4 limiter bounds each
tenant, actor, and action, including denied authorization probes.

## Request contract

- Request bodies have a configured byte ceiling and are rejected before being
  read or decoded when their declared length exceeds it.
- Every request receives a nonempty request ID and a cooperative deadline.
  Repository loops and adapters must call `HostedRequestContext.check()` at
  bounded intervals and before committing a response. Client disconnects call
  `cancel()`; cancellation never implies that an already committed mutation was
  rolled back.
- API versions are explicit and drawn from a configured supported tuple.
  Missing versions resolve to the first version only while v1 compatibility is
  supported. Unknown versions fail with `unsupported_api_version`; they never
  silently downgrade.
- CORS uses exact allowed-origin matching. The default is no browser origins;
  credentials and wildcard origins are not combined.
- Errors use stable machine codes, HTTP status, request ID, and an optional
  retry delay. They do not include stack traces, SQL, credentials, memory
  content, or foreign-resource existence.

## Pagination and idempotency

Hosted list routes use `HostedCursorCodec`. Cursors are opaque, HMAC-signed,
expiring, versioned, and bound to one tenant and route. A cursor from another
tenant or route, a modified cursor, and an expired cursor all produce the same
`invalid_cursor` result.

Mutation routes require an idempotency key. `HostedIdempotencyStore` scopes it
to tenant and operation, stores only a canonical request digest plus the final
JSON response, and persists replay state across process restarts for a bounded
retention interval. Concurrent identical claims return
`idempotency_in_progress`; reuse with a different request returns
`idempotency_conflict`. Expired rows are removed transactionally before a new
claim. Adapters must complete the idempotency record only after the underlying
mutation commits.

The first mutation route uses PUT-like project-create semantics: provisioning
the same tenant/project root is harmless. A denied or failed mutation abandons
only its matching pending claim so a corrected request can retry. Once the
mutation and replay response commit, a late deadline or disconnect may hide the
response from that client but a retry returns the stored result instead of
executing a second mutation.

## Remaining P7-5 integration gate

The policy primitives and concrete adapter are dependency-free and tested, but
no public hosted listener exists yet. Direct adapter tests cover trusted-proxy
TLS resolution, CORS/version metadata, oversized and malformed bodies,
cross-tenant denial, cursor isolation, persistent mutation replay, deadline
expiry, and disconnect cancellation. Before P7-5 can pass, a deployment-shaped
listener test must prove socket-level body/read deadlines, disconnect
propagation, trusted proxy/TLS configuration, and graceful shutdown. P7-4
authorization remains mandatory after all transport checks.
