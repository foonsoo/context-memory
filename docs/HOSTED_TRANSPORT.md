# Hosted transport and API contract

The existing stdio server and localhost HTTP integration are not the hosted
service transport. They retain their current local-first support boundary.
Any future hosted adapter must compose the contracts in
`context_memory.hosted_transport` before dispatching to the P7-4 identity and
repository gateways. Merely binding the local bearer-token server to a public
interface is unsupported.

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

## Remaining P7-5 integration gate

The policy primitives are dependency-free and tested, but no public hosted
listener exists yet. Before P7-5 can pass, a concrete adapter must apply every
guard end to end, translate disconnects into cooperative cancellation, exercise
slow/oversized/malformed requests, and demonstrate trusted-proxy/TLS behavior
in a deployment-shaped test. P7-4 authorization remains mandatory after these
transport checks.
