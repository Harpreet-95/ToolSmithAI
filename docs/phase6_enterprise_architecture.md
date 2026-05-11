# ToolSmithAI — Phase 6 Enterprise Architecture Plan

**Status:** Planning
**Scope:** Architecture only — no implementation details or code

---

## 1. Phase 6 Objective

Phase 6 transforms ToolSmithAI from a single-tenant MVP backend into a production-grade, multi-tenant platform capable of supporting external partners, API consumers, and enterprise clients. The goal is to evolve the existing interpreter pipeline into a commercially viable automation-as-a-service layer — without discarding or rewriting the core interpreter, execution engine, or audit infrastructure already built.

The three pillars of Phase 6 are:

- **Multi-tenancy at scale** — full data and config isolation between tenants
- **Universal API integration** — allow tenants to connect real external tools (email, Slack, databases, webhooks) without touching core code
- **Monetisation infrastructure** — usage metering, plan enforcement, and partner billing hooks

---

## 2. Current System Foundation

Phase 6 builds directly on the following completed foundations:

| Capability | Status |
|---|---|
| Natural language task interpreter | Complete |
| Rule-based execution engine with retry logic | Complete |
| Stored workflow system (create, run by name/ID) | Complete |
| FastAPI + SQLite persistence layer | Complete |
| Dual-write audit logging (file + SQLite) | Complete |
| Fernet encryption at rest for sensitive fields | Complete |
| HMAC-derived user identity (key never stored) | Complete |
| Role-based access control (admin / user) | Complete |
| Auth failure rate limiting | Complete |
| Global exception handling and error standardisation | Complete |
| CORS middleware | Complete |
| Docker containerisation | Complete |
| Render deployment (HTTPS, free tier) | Complete |
| tenant_id column in audit_logs and execution_history | Complete (migration 007) |
| tenant_id in AuthenticatedUser and TENANT_ID_MAP | Complete |
| tenant_id propagation to data write functions | Complete |

The outstanding Phase 6A items (propagating tenant_id through routes, input handler, and workflow runner, and filtering insights/recommendations by tenant) must be completed before Phase 6B begins.

---

## 3. Multi-Tenant Expansion Plan

### 3.1 Tenant Identity Model

Each tenant is a discrete organisational boundary. Within a tenant, there can be multiple users. The identity hierarchy is:

```
Tenant
  └── Users (one or many API keys per tenant)
        └── Roles (admin, user, read-only)
```

Currently, tenants are identified by a static string from env vars. Phase 6 moves tenant and user records into the database, making them fully dynamic.

### 3.2 Tenant Database Record

A new `tenants` table stores tenant metadata: name, plan tier, status (active/suspended), creation date, and configuration overrides (retention days, rate limits, allowed tools).

### 3.3 API Key Management

API keys move from env vars into a dedicated `api_keys` table. Each key belongs to a tenant and carries a role. Keys can be created, rotated, and revoked via admin API without a redeploy. The HMAC derivation approach for user_id is preserved exactly — only the key store changes.

### 3.4 Data Isolation Strategy

All tenant data is isolated by `tenant_id` at the query layer. No row-level security is implemented at the SQLite/Postgres level in Phase 6 — isolation is enforced in application code. Every query against `audit_logs`, `execution_history`, and `workflows` includes a `WHERE tenant_id = ?` clause. The admin superuser role (platform-level, distinct from tenant admin) can query across tenants.

### 3.5 Tenant-Scoped Configuration

Each tenant can have overrides for: retention policy, rate limits, maximum workflow steps, allowed tool categories, and maximum concurrent executions. These are stored as JSON in the `tenants` table and loaded at request time rather than from env vars.

---

## 4. Universal API Integration Layer

### 4.1 Problem

The current execution engine uses hardcoded simulated handlers (`handle_send_email`, `handle_fetch_report_data`, etc.). Phase 6 replaces this with a dynamic integration registry that allows tenants to connect real external services.

### 4.2 Integration Registry

A new `integrations` table stores per-tenant connector configurations: service type (email, Slack, webhook, SQL, HTTP), connection parameters (encrypted), and enabled status. The execution engine resolves a tool dispatch key against this table at runtime rather than against a hardcoded dict.

### 4.3 Connector Categories

| Category | Examples |
|---|---|
| Email | SMTP, SendGrid, Mailgun |
| Notification | Slack, Teams, PagerDuty, webhook POST |
| Data fetch | PostgreSQL query, REST API GET, CSV pull |
| Data write | REST API POST, database INSERT |
| File | S3 put/get, local file write |
| Custom | Arbitrary HTTP endpoint with auth headers |

### 4.4 Connector Execution Model

The execution engine's dispatch table becomes a two-level lookup: first check for a tenant-specific connector override, then fall back to the built-in simulated handler. This ensures backward compatibility while enabling real integrations per tenant.

### 4.5 Credential Security

All connector credentials (API tokens, SMTP passwords, webhook secrets) are encrypted at rest using the same Fernet infrastructure already in place. Decryption happens only at execution time, in memory, and is never logged.

---

## 5. Partner Ecosystem Model

### 5.1 Partner Tiers

Three partner tiers are planned:

| Tier | Description |
|---|---|
| Developer | Free tier, rate-limited, single tenant, limited tool categories |
| Business | Paid, higher limits, multi-user tenant, custom connectors |
| Enterprise | SLA-backed, dedicated infrastructure option, custom retention, audit export |

### 5.2 Partner Onboarding API

A partner onboarding flow (admin-only endpoints) allows platform admins to:
- Create a new tenant record
- Issue the first API key for that tenant
- Assign a plan tier
- Configure allowed tool categories

This removes the need for env var changes or redeployment when adding a new partner.

### 5.3 Tenant Admin Endpoints

Each tenant's designated admin user gains access to tenant-scoped admin endpoints:
- List and revoke API keys within their tenant
- View their tenant's audit logs and execution history
- Configure integrations for their tenant
- View their own usage metrics

Platform-level admin (superuser) retains cross-tenant visibility.

---

## 6. Usage Analytics and Monetisation Layer

### 6.1 Usage Metering

A new `usage_events` table records billable events per tenant: API calls by endpoint, execution steps run, integrations triggered, and data volume processed. This table is append-only and never purged by the standard retention policy.

### 6.2 Plan Enforcement

At request time, a plan enforcement layer checks the tenant's current period usage against their plan limits before executing. If a limit is exceeded, the request is rejected with a structured `429 Plan Limit Exceeded` response rather than a generic rate limit error.

### 6.3 Billing Integration Points

Phase 6 does not implement a billing system directly. Instead, it exposes:
- A `GET /admin/tenants/{id}/usage` endpoint returning metered usage per billing period
- A webhook hook that fires when a tenant crosses a usage threshold
- An export endpoint for usage data in CSV format

These are designed to integrate with Stripe, Lago, or any external billing system.

### 6.4 Usage Dashboard Endpoint

A new `GET /v1/usage/summary` endpoint (tenant-scoped) returns the calling tenant's usage for the current billing period: total interpret calls, workflow runs, step executions, and remaining allowance.

---

## 7. Scaling and Deployment Plan

### 7.1 Current Deployment Limitation

The current Render free-tier deployment uses a single process, single SQLite file, and spins down on inactivity. This is unsuitable for production multi-tenant workloads.

### 7.2 Phase 6 Target Deployment

| Component | Target |
|---|---|
| App servers | Multiple Uvicorn workers behind a load balancer |
| Database | PostgreSQL (managed, see Section 8) |
| Cache | Redis (see Section 9) |
| Background workers | Celery or ARQ for async execution |
| File storage | S3-compatible object store for audit log archives |
| Secrets management | Environment-injected via platform secret store (Render, Railway, or AWS Secrets Manager) |

### 7.3 Execution Async Model

Currently, task execution is synchronous — the HTTP response waits for the full plan to execute. For long-running workflows, Phase 6 introduces an async execution path:
- `POST /v1/interpret` returns a `job_id` immediately
- Execution runs in a background worker
- `GET /v1/jobs/{job_id}` polls for result
- Optional webhook callback when execution completes

The synchronous path is preserved for short tasks to maintain backward compatibility.

### 7.4 Horizontal Scaling Constraints

SQLite cannot support multiple concurrent writers across processes. Moving to PostgreSQL (Section 8) is a prerequisite for horizontal scaling. All other application code — routes, auth, interpreter, execution engine — is stateless and scales horizontally without change.

---

## 8. Database Evolution: SQLite to PostgreSQL

### 8.1 Why PostgreSQL

| Requirement | SQLite limitation | PostgreSQL capability |
|---|---|---|
| Multiple app server processes | Single writer only | Full concurrent write support |
| Row-level locking | Table-level only | Row-level, minimal contention |
| Full-text search on audit logs | Very limited | Native `tsvector` / `GIN` index |
| JSON column operations | Basic | Full `jsonb` operators and indexing |
| Connection pooling | Not supported | PgBouncer or built-in pooling |
| Managed backups and PITR | Manual only | Automated on all managed providers |

### 8.2 Migration Strategy

The migration from SQLite to PostgreSQL is a two-phase approach:

**Phase 6B:** Run both databases in parallel. New writes go to PostgreSQL. Reads fall back to SQLite for historical data. A one-time migration script backfills existing rows from SQLite into PostgreSQL.

**Phase 6C:** SQLite is decommissioned. All reads and writes go to PostgreSQL exclusively.

### 8.3 Schema Changes for PostgreSQL

The column types remain largely compatible. Key changes:
- `INTEGER PRIMARY KEY AUTOINCREMENT` becomes `SERIAL PRIMARY KEY` or `BIGSERIAL`
- `TEXT` columns storing JSON (`definition`, `config`) become `JSONB`
- `user_id` and `tenant_id` columns gain indexes for query performance
- `timestamp` columns become `TIMESTAMPTZ` (timezone-aware)

### 8.4 Connection Management

The current `get_connection()` helper in `data/db.py` is replaced by a connection pool (SQLAlchemy Core or `asyncpg` for async paths). The rest of the data layer is unchanged — query strings require only minor syntax adjustments.

---

## 9. Redis / Cache and Queue Strategy

### 9.1 Caching Use Cases

| Data | Cache strategy | TTL |
|---|---|---|
| Tenant config and plan limits | Cache on first load, invalidate on update | 5 minutes |
| `KEY_ROLE_MAP` and `TENANT_ID_MAP` | Cache in memory at startup, refresh on key rotation event | Per deploy |
| Workflow definitions | Cache by name/ID per tenant | 2 minutes |
| `/v1/insights` results | Cache per tenant | 10 minutes |
| `/v1/recommendations` results | Cache per tenant | 10 minutes |

### 9.2 Rate Limiting with Redis

The current `AuthFailureRateLimiter` middleware uses in-process state — it resets on every deploy and does not work across multiple app server instances. Phase 6 moves rate limit counters into Redis so limits are shared across all instances.

### 9.3 Job Queue

Async task execution (Section 7.3) requires a durable job queue. Redis is used as the Celery or ARQ broker. Each job carries: `tenant_id`, `user_id`, `plan`, `callback_url` (optional), and a `job_id`. Workers pull from the queue, execute the plan, and write the result to the database. The HTTP layer only enqueues and polls.

### 9.4 Redis Deployment

A single Redis instance (managed, e.g. Redis Cloud free tier or Render Redis) is sufficient for Phase 6 workloads. Sentinel or cluster mode is deferred to Phase 7.

---

## 10. Security and Enterprise Auth Roadmap

### 10.1 Current Auth Limitations

- Two global API keys from env vars (no per-user key management)
- No key expiry or rotation mechanism
- No audit of key creation or revocation
- No support for OAuth, SSO, or JWT

### 10.2 Phase 6 Auth Enhancements

**API key management:**
Keys move to the database. Each key has: `tenant_id`, `user_id`, `role`, `created_at`, `expires_at` (nullable), `last_used_at`, `revoked` flag. The HMAC derivation for `user_id` is preserved. Key lookup moves from iterating a 2-entry dict to a single indexed DB query.

**Key expiry:**
The auth layer checks `expires_at` on every request. Expired keys receive a `401` with a message distinguishing expiry from invalidity.

**Key rotation:**
Tenants can issue a new key and set the old key's `expires_at` to a short grace period (e.g. 24 hours), allowing a smooth rotation without instant lockout.

**Audit of auth events:**
Key creation, rotation, and revocation are written to `audit_logs` as structured events with `task_type = "key_management"`.

### 10.3 Future Auth (Phase 7+)

- OAuth 2.0 / OpenID Connect for enterprise SSO
- JWT issuance after OAuth login (short-lived tokens, refresh via Redis)
- IP allowlist per tenant
- Mutual TLS for partner-to-partner API calls
- Signed webhook payloads (HMAC-SHA256 on outbound callbacks)

### 10.4 Compliance Considerations

- All PII fields remain Fernet-encrypted at rest
- Retention policy becomes per-tenant configurable (not global)
- Audit log export endpoint for compliance reporting
- Right-to-erasure endpoint (already partially implemented via `DELETE /v1/me/data`) extended to cover all tenant data
- GDPR-aligned data residency option (deferred — requires multi-region deployment)

---

## 11. Recommended Phase 6 Build Order

Ordered by dependency and risk:

| Step | What | Why first |
|---|---|---|
| 6A (complete remaining) | Propagate `tenant_id` through routes, input handler, workflow runner; filter insights/recommendations by tenant | Unblocks all downstream tenant isolation |
| 6B | Move API keys to database; build key creation/revocation admin endpoints | Unblocks per-tenant key management and partner onboarding |
| 6C | Build tenant management endpoints (create tenant, assign plan, configure limits) | Unblocks partner onboarding without env var changes |
| 6D | Implement plan enforcement layer (check usage limits before execution) | Required before monetisation |
| 6E | Build `usage_events` metering table and usage summary endpoint | Required before billing integration |
| 6F | Migrate rate limiter to Redis; add Redis-backed job queue; introduce async execution path | Required before horizontal scaling |
| 6G | Replace SQLite with PostgreSQL (parallel write phase, then cutover) | Required for multi-instance deployment |
| 6H | Build universal integration registry (tenant-configured connectors replacing hardcoded handlers) | Required for real tool executions |
| 6I | Expose billing webhook hooks and usage export endpoint | Final monetisation wiring |
| 6J | Enterprise auth hardening — key expiry, rotation, IP allowlist | Final security tier |

---

## Design Principles for Phase 6

- **Additive over destructive** — every Phase 6 change should extend existing interfaces, not replace them. Existing API consumers must not break.
- **Tenant isolation by default** — every query that touches user data must include a `tenant_id` filter. No exceptions.
- **Encrypt all credentials** — any connector secret, partner token, or integration credential is Fernet-encrypted before DB write.
- **One audit trail** — all platform events (auth, execution, key management, config changes) flow through the existing `log_audit_event()` path. No parallel logging systems.
- **Stateless app layer** — all shared state (rate limits, cache, sessions) lives in Redis or the database, never in process memory, so horizontal scaling works without coordination.
