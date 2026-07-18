# API Reference

## Conventions

The product API is rooted at `/api/v1`. Built-in Swagger/ReDoc routes and the default public OpenAPI
route are disabled. The machine-readable schema is available through the same protected router at
`GET /api/v1/openapi.json`; `X-API-Key` is required there when an application key is configured.

Requests and responses use JSON unless an endpoint explicitly accepts multipart upload. Timestamps are UTC ISO 8601 strings. Repository and job IDs are opaque strings; clients must not derive paths, collection names, or authorization decisions from their format.

### Authentication

When `CODEBASE_INTEL_API_KEY` is non-empty, protected endpoints require:

```http
X-API-Key: configured-application-key
```

Only these endpoints remain public:

- `GET /`
- `GET /api/v1/health/live`
- `GET /api/v1/health/ready`

The API key is a single-deployment shared secret, not a user identity or tenant-authorization system.

Private GitHub acquisition optionally accepts a second request-scoped header:

```http
X-GitHub-Token: least-privilege-fine-grained-token
```

That token applies only to `POST /api/v1/repositories`. It must not be included in the JSON body, URL, query string, general application configuration, or subsequent job requests.

### Request IDs and errors

The API attaches a request ID to responses. A client-supplied request ID may be accepted only after validation; otherwise the server generates one. Log or report the ID, never a credential or repository body.

Application errors use RFC 9457-shaped JSON:

```json
{
  "type": "about:blank",
  "title": "Invalid State",
  "status": 409,
  "detail": "Repository must be ready before it can answer questions.",
  "code": "INVALID_STATE",
  "request_id": "opaque-request-id"
}
```

Expected status classes include:

| Status | Meaning |
|---:|---|
| `200` | Successful read or action with a response body |
| `202` | Repository ingestion or reindex queued |
| `204` | Repository and associated local state deleted |
| `400` | Invalid source or unsafe operation |
| `401` | Missing or invalid configured application API key |
| `404` | Repository or job does not exist |
| `409` | Resource state does not permit the operation |
| `413` | Upload or extracted content exceeds a configured boundary |
| `422` | Request schema validation failed |
| `502` | GitHub acquisition failed upstream |
| `503` | Required provider or dependency is unavailable |

Remote provider bodies, credentials, source content, and stack traces are not part of the problem response.

## Service and health

### `GET /`

Public service metadata and pointers to the protected schema and public health endpoints. Use the health endpoints, not this route, for orchestration:

```json
{
  "service": "Codebase Intelligence",
  "version": "0.2.0",
  "openapi": "/api/v1/openapi.json",
  "liveness": "/api/v1/health/live",
  "readiness": "/api/v1/health/ready"
}
```

### `GET /api/v1/health/live`

Public process liveness:

```json
{
  "status": "ok",
  "checks": {
    "process": true
  }
}
```

Liveness does not inspect providers, SQLite, Qdrant, the worker, or queued jobs.

### `GET /api/v1/health/ready`

Public dependency readiness. The `database` check requires the complete `(1, 2, 3)` SQLite
migration set, `embedding` requires both provider configuration and successful runtime
initialization, and `qdrant` performs a collection-list health call through the initialized vector
client. When the API is configured to own the inline worker, a conditional `worker` check requires
that task to exist and remain running. The endpoint returns HTTP `503` with `status: "degraded"`
when any required check is false:

```json
{
  "status": "ok",
  "checks": {
    "database": true,
    "qdrant": true,
    "embedding": true,
    "worker": true
  }
}
```

The `worker` key is omitted when this process is not expected to own an inline worker. The readiness
response does not prove a separately supervised external worker or that queued work is advancing;
it also excludes optional OpenAI synthesis. The answer path is reported separately because indexing
and extractive fallback can remain available when synthesis is unavailable. Operators should
inspect the response body, `/status`, and job freshness rather than treating an HTTP connection
alone as readiness.

### `GET /api/v1/status`

Protected, non-secret runtime configuration:

```json
{
  "application": "Codebase Intelligence",
  "version": "0.2.0",
  "environment": "development",
  "embedding": {
    "provider": "deterministic",
    "model": "deterministic-hash-v1",
    "ready": true,
    "mode": "demo"
  },
  "answer": {
    "provider": "extractive",
    "model": "ranked-source-extracts",
    "ready": true,
    "mode": "demo"
  },
  "qdrant_mode": "embedded",
  "inline_worker": true
}
```

The configured provider/model and demo/production label come from settings; each `ready` flag comes
from the initialized runtime path, not merely the presence of a credential string. No key value or
provider token is returned. A green provider flag proves initialization only, not a live paid
provider request or provider quality.

### `GET /api/v1/openapi.json`

Returns the OpenAPI schema for authenticated tooling. When `CODEBASE_INTEL_API_KEY` is configured, supply `X-API-Key`. This project does not expose an unauthenticated interactive documentation UI.

## Repositories

Repository status is one of `queued`, `indexing`, `ready`, `failed`, or `deleting`.

### `GET /api/v1/repositories`

Returns a JSON array of repository records, newest first. Repository records include source metadata, state, optional commit/collection/fingerprint values, safe failure details, timestamps, and statistics:

```json
[
  {
    "id": "opaque-repository-id",
    "name": "payments-service",
    "status": "ready",
    "source_kind": "github",
    "source_url": "https://github.com/acme/payments-service",
    "source_ref": "main",
    "commit_sha": "40-character-commit-sha",
    "collection_name": "codebase_intel_opaqueid_a1b2c3d4e5f6",
    "index_fingerprint": "sha256-fingerprint",
    "stats": {
      "file_count": 42,
      "chunk_count": 180,
      "skipped_file_count": 3,
      "tree_sitter_file_count": 39,
      "fallback_file_count": 3,
      "redaction_count": 1,
      "indexed_bytes": 96000,
      "languages": {"python": 31, "typescript": 11}
    },
    "error_code": null,
    "error_message": null,
    "created_at": "2026-07-17T12:00:00Z",
    "updated_at": "2026-07-17T12:01:00Z"
  }
]
```

### `POST /api/v1/repositories`

Queues a GitHub repository after the API has acquired a bounded archive and durably moved it into
the repository directory. The repository manifest and ingest job are then inserted in one SQLite
transaction, so a successful `202` cannot expose only one of those records.

```json
{
  "url": "https://github.com/acme/payments-service",
  "ref": "main",
  "name": "payments-service"
}
```

- `url` is required and must be a canonical `https://github.com/<owner>/<repository>` URL.
- `ref` is optional and may identify a branch, tag, or commit.
- `name` is an optional display override.
- `X-GitHub-Token` is optional and request-scoped for private repositories.

Success is `202 Accepted`:

```json
{
  "repository_id": "opaque-repository-id",
  "job_id": "opaque-job-id",
  "status": "queued"
}
```

### `POST /api/v1/repositories/upload`

Accepts `multipart/form-data`:

| Field | Required | Meaning |
|---|---|---|
| `file` | Yes | One bounded `.zip` archive |
| `name` | No | Display name override |

The filename must be a safe basename. Archive safety and extracted-content limits are enforced
independently of the multipart size. After the archive is durable, repository/job persistence uses
the same atomic lifecycle transaction and `202` response as GitHub ingestion.

### `GET /api/v1/repositories/{repository_id}`

Returns one repository record. `404` means the ID does not exist; clients should not infer deletion success solely from a missing UI card.

### `GET /api/v1/repositories/{repository_id}/sources`

Returns a bounded, path-sorted file catalog reconstructed from the repository's active redacted Qdrant collection. The endpoint applies the same ready-state, current-index-fingerprint, physical-collection, and repository-scope checks as question retrieval.

Optional query parameters:

| Parameter | Default | Boundary | Meaning |
|---|---:|---:|---|
| `q` | empty | 100 characters | Case-insensitive match across indexed path, symbol, or redacted content |
| `language` | empty | 50 characters | Exact case-insensitive indexed language filter |
| `limit` | `200` | `1`–`500` | Maximum file summaries returned |

```json
{
  "repository_id": "opaque-repository-id",
  "collection_name": "codebase_intel_opaqueid_a1b2c3d4e5f6",
  "total": 1,
  "files": [
    {
      "path": "src/auth/session.py",
      "language": "python",
      "chunk_count": 2,
      "symbol_count": 2,
      "start_line": 1,
      "end_line": 48
    }
  ]
}
```

`total` is the complete filtered file count before `limit` is applied. An empty `files` array is a successful no-match result. The response does not read the raw archive or extracted snapshot.

### `GET /api/v1/repositories/{repository_id}/source`

Returns ordered indexed sections for one exact repository-relative path. Supply the required `path` query parameter and let the HTTP client encode nested paths:

```http
GET /api/v1/repositories/opaque-repository-id/source?path=src%2Fauth%2Fsession.py
```

```json
{
  "repository_id": "opaque-repository-id",
  "collection_name": "codebase_intel_opaqueid_a1b2c3d4e5f6",
  "path": "src/auth/session.py",
  "language": "python",
  "sections": [
    {
      "chunk_id": "stable-application-chunk-id",
      "path": "src/auth/session.py",
      "language": "python",
      "symbol": "authenticate_request",
      "symbol_kind": "function",
      "start_line": 12,
      "end_line": 29,
      "parser": "tree_sitter",
      "content": "def authenticate_request(request):\n    return sessions.verify(request)"
    }
  ],
  "truncated": false
}
```

The returned content is the already-redacted text stored in the active index. Responses are bounded by section and indexed-line limits; `truncated` indicates that additional indexed sections were omitted. A path absent from that repository's active index returns `404`. A non-ready, stale-fingerprint, or physically missing active index returns `409` through the standard problem envelope.

### `DELETE /api/v1/repositories/{repository_id}`

Synchronously rejects a running job, cancels queued work, and removes every versioned Qdrant
collection identified by the repository UUID marker across configured-prefix histories, stored
repository files, jobs, and the manifest record. It returns `204 No Content` only after every exact
collection target and the repository path pass readback, followed by an acknowledged one-row
manifest delete commit whose foreign-key cascade removes jobs.
Backup/provider retention is outside this endpoint's local deletion scope.

### `POST /api/v1/repositories/{repository_id}/reindex`

Queues a reindex of the existing immutable archive or extracted snapshot. It does not refetch a
moving GitHub branch. In one SQLite transaction, the repository moves from `ready` or `failed` to
`indexing` and exactly one new reindex job is inserted. Use it after changing the embedding
model/dimension, Tree-sitter/redaction/rerank/chunk contract, Qdrant collection prefix, or after a
recoverable indexing failure.

Success is `202 Accepted` with `repository_id`, new `job_id`, and repository `status: "indexing"`.
A partial unique database index permits at most one `queued` or `running` job for the repository, so
a repository with no usable snapshot, an incompatible state, or existing active work returns `409`.

Reindex builds a new physical collection while the last published collection remains persisted.
Successful publication stores the new collection/fingerprint and succeeds the live job atomically,
then attempts best-effort removal of old versions. If retries end in failure, cleanup of the
unpublished collection is also best-effort and a single transaction fails the job and reconciles
the repository. A reindex returns to `ready` only when its prior published collection passes
physical readback; otherwise, like an initial ingest with no publication, the repository becomes
`failed`. The restored repository keeps the prior fingerprint/stats, so `ready` preserves last-good
bytes but questions still return `409` when current index settings differ.

### `POST /api/v1/repositories/{repository_id}/questions`

The repository must be `ready`. It must also have a persisted collection name and an index
fingerprint equal to the complete current index contract. A missing name, mismatched
fingerprint, or absent physical collection returns `409`; the absent-collection case uses
`INDEX_MISSING`. Reindex is required, and the service never guesses another collection.

```json
{
  "question": "How does the payment flow work?",
  "top_k": 8,
  "history": [
    {"role": "user", "content": "Focus on checkout."},
    {"role": "assistant", "content": "I will trace the cited path."}
  ]
}
```

- `question` is 3–4,000 characters.
- `top_k` is 1–20 and defaults to 8.
- `history` contains at most 12 user/assistant messages; it is context, not an authorization or source boundary.

Response:

```json
{
  "answer": "The strongest repository evidence is in these locations:\n- `src/payments.py:10-28` (`capture_payment`) [S1]",
  "answer_mode": "extractive",
  "citations": [
    {
      "source_id": "S1",
      "repository_id": "opaque-repository-id",
      "commit_sha": "40-character-commit-sha",
      "path": "src/payments.py",
      "language": "python",
      "symbol": "capture_payment",
      "symbol_kind": "function",
      "start_line": 10,
      "end_line": 28,
      "score": 0.87,
      "retrieval_signals": {
        "semantic_score": 0.87,
        "combined_score": 2.445,
        "path_overlap": 0.25,
        "symbol_overlap": 0.5,
        "content_overlap": 0.6
      },
      "excerpt": "def capture_payment(...): ...",
      "permalink": "https://github.com/acme/payments-service/blob/commit/src/payments.py#L10-L28"
    }
  ],
  "repository_id": "opaque-repository-id",
  "question": "How does the payment flow work?"
}
```

Search targets the exact collection persisted on the repository record and rejects any retrieved
payload whose repository ID does not match. `answer_mode` is `openai` only when synthesis returned
valid known citation IDs; otherwise the service falls back to `extractive`. An unanswerable question
returns an insufficient-evidence answer and an empty citation list rather than inventing a source
location.

`retrieval_signals` is additive and may be absent on older stored/client fixtures. `semantic_score`
is the bounded vector similarity while path, symbol, and content values are lexical overlap ratios.
`combined_score` is the deterministic weighted value used to rank candidates. These fields explain
retrieval; none is a calibrated probability or model-confidence claim.

## Jobs

Job status is `queued`, `running`, `succeeded`, `failed`, or `cancelled`. Stage is one of `queued`,
`fetching`, `extracting`, `scanning`, `parsing`, `embedding`, `indexing`, `deleting`, or `complete`.
The database allows at most one job in the shared active set (`queued` or `running`) per repository;
terminal job history is retained.

### `GET /api/v1/jobs`

Returns a JSON array of jobs, newest first. Optional query parameters:

| Parameter | Default | Constraint |
|---|---:|---|
| `repository_id` | none | Exact opaque repository ID |
| `status` | none | One job status value |
| `limit` | `100` | 1–1,000 |
| `offset` | `0` | Non-negative |

Each record contains ID, repository ID, kind, status, stage, 0–100 progress, attempt count, sanitized failure data, lease/timestamp state, and a non-secret job payload. Clients must not depend on lease-owner values as a stable worker identity.

Worker heartbeats use a lease-only renewal operation: they update lease expiry without replaying
stage/progress. Expired running jobs are requeued until the configured attempt limit; exhausted jobs
and their `queued`/`indexing` repositories fail. Stale recovery does not trust a persisted
collection-name string without Qdrant readback; an operator may explicitly reindex the failed
repository from its immutable snapshot.

### `GET /api/v1/jobs/{job_id}`

Returns one job or `404`.

### `POST /api/v1/jobs/{job_id}/cancel`

Atomically cancels a `queued` job before a worker can claim it and returns the updated job record.
Cancelling `running` or terminal work is a `409` illegal transition; there is no remote interruption
of an active provider call. Cancellation does not change repository state; startup reconciliation
may enqueue replacement work for a `queued`/`indexing` repository that still has its snapshot and
no active job. Delete the repository to stop and remove it, or wait for running work to finish before
deletion.

## Client guidance

- Treat every `202` response as queued work and poll with bounded backoff.
- Use repository/job state from the API, not optimistic UI state.
- Do not retry `400`, `401`, `404`, `409`, `413`, or `422` unchanged.
- Retry transient `503` and transport failures with jitter and a limit; respect provider budgets.
- Reconcile a timed-out create/delete by reading current repository/job state before repeating the mutation.
- Render `answer` as untrusted model text and citations as navigation evidence, not executed markup.
- Never log request bodies for questions, upload bytes, `X-API-Key`, or `X-GitHub-Token`.
