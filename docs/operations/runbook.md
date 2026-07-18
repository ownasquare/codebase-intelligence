# Operations Runbook

## Operating modes

Codebase Intelligence supports two deliberate topologies:

| Mode | API/worker | Qdrant | Intended use |
|---|---|---|---|
| Local inline | One FastAPI process runs the inline worker | Embedded under `.data/qdrant` | Development, deterministic demo, focused tests |
| Service | Separate API and worker processes | Qdrant Server | Docker Compose and production-shaped deployments |

Do not run separate API and worker processes against embedded Qdrant storage. Configure `CODEBASE_INTEL_QDRANT_URL` whenever more than one process needs vector access.

## Configuration baseline

Settings use the `CODEBASE_INTEL_` prefix except the provider-standard `VOYAGE_API_KEY` and `OPENAI_API_KEY` variables. Start from `.env.example` for local work; production values should come from a managed secret store.

Critical settings:

| Setting | Local demo | Service guidance |
|---|---|---|
| `CODEBASE_INTEL_DATA_DIR` | `.data` | Durable local filesystem mounted at `/data` |
| `CODEBASE_INTEL_EMBEDDING_PROVIDER` | `deterministic` | `voyage` or `openai` after data-policy review |
| `CODEBASE_INTEL_ANSWER_PROVIDER` | `extractive` | `extractive` or `openai` |
| `CODEBASE_INTEL_QDRANT_URL` | empty | Qdrant Server URL, e.g. `http://qdrant:6333` |
| `CODEBASE_INTEL_INLINE_WORKER` | `true` | `false` with a separately supervised worker |
| `CODEBASE_INTEL_API_KEY` | optional on loopback | Required beyond a private single-user boundary |
| `CODEBASE_INTEL_ALLOWED_ORIGINS` | local Streamlit origins | Exact trusted UI origins, never a wildcard with secrets |

The selected embedding credential must be present in both service-mode API and worker processes:
the worker embeds indexed chunks, while the API initializes the vector client and embeds question
queries. The OpenAI answer credential must also be present in the API when synthesis is selected.
The worker must not receive private GitHub tokens; GitHub acquisition occurs in the API, which
persists a local repository snapshot before it creates the token-free job.

## Local inline startup

Install the exact lock:

```bash
make sync
```

Start a credential-free API:

```bash
CODEBASE_INTEL_EMBEDDING_PROVIDER=deterministic \
CODEBASE_INTEL_ANSWER_PROVIDER=extractive \
CODEBASE_INTEL_INLINE_WORKER=true \
make api
```

In another terminal:

```bash
CODEBASE_INTEL_API_BASE_URL=http://127.0.0.1:8000 make ui
```

Verify process and dependency state:

```bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/health/live
curl --fail-with-body http://127.0.0.1:8000/api/v1/health/ready
```

Stop both processes with their terminal interrupt before copying, upgrading, or restoring `.data`.

## Separate local services

For API/worker debugging without Compose, start Qdrant Server first and set the same `CODEBASE_INTEL_DATA_DIR`, `CODEBASE_INTEL_QDRANT_URL`, provider configuration, and API key in both application processes.

Terminal 1:

```bash
CODEBASE_INTEL_INLINE_WORKER=false make api
```

Terminal 2:

```bash
CODEBASE_INTEL_INLINE_WORKER=false make worker
```

The data directory must be on a local filesystem with reliable SQLite locking. Do not place the manifest on an arbitrary NFS/object-store mount.

## Docker Compose startup

Validate resolved configuration before starting:

```bash
docker compose config
```

Review the rendered output for provider mode, public ports, volume names, and secret-source integration. Do not paste rendered configuration into tickets because an operator may have supplied secrets.

Build and start:

```bash
make compose-up
docker compose ps
```

Expected topology:

- `ui` publishes only `127.0.0.1:8501`.
- `api` is reachable by UI as `http://api:8000` but has no host port.
- `qdrant` is attached only to the internal `data` network and has no host port.
- `qdrant-probe` uses the application image to check Qdrant `/readyz`; the deliberately minimal Qdrant image does not need a shell or HTTP client.
- `worker` and `api` share `codebase-intelligence-data`.
- Qdrant storage and snapshots persist to `codebase-intelligence-qdrant` and `codebase-intelligence-qdrant-snapshots`; anonymous Qdrant usage telemetry is disabled.
- API, worker, and UI use the same non-root application image; roots are read-only except named volumes and `tmpfs`.

Open `http://127.0.0.1:8501`. Stop without removing state:

```bash
make compose-down
```

Never add `--volumes` to routine shutdown. Volume deletion is a separate destructive retention decision.

## Health and monitoring

### Probes

- `/api/v1/health/live` proves only that the API process can respond.
- `/api/v1/health/ready` requires exactly the applied SQLite migration set `(1, 2, 3)`, an
  operational configured embedding path, and a successful Qdrant collection-list health call. When
  the API owns an inline worker, a conditional `worker` check also requires that task to exist and
  remain running. It reports `ok` on success and returns HTTP `503` with `degraded` when any check
  fails.
- `/api/v1/status` reports configured provider/model, runtime initialization readiness, demo versus
  production provider mode, Qdrant mode, and inline-worker mode. It is API-key protected when
  authentication is configured. A green provider flag is not proof that a live paid request passed.
- Streamlit exposes `/_stcore/health` inside its container.
- The worker container health check proves PID 1 liveness only. Job freshness and queue health come from `/api/v1/jobs`.

### Operational signals

Monitor at least:

- readiness body and duration;
- queued/running job age, stage, attempt count, and failure code;
- stale-lease recovery, lease-owner loss, and exhausted attempts;
- repository counts by state;
- filesystem bytes/inodes, versioned Qdrant collection/storage growth, and multiple inactive
  versions bearing one repository UUID marker, including versions from older configured prefixes;
- provider latency, throttling, quota, and safe error class;
- upload rejection by boundary;
- deletion failures or orphan-repair activity; and
- container restart and OOM events.

Questions, source text, archive contents, token headers, provider payloads, and remote error bodies must not become metrics labels or logs. Use request/repository/job IDs and bounded error codes.

## Repository lifecycle operations

### Ingest

1. Submit a GitHub URL or ZIP.
2. Record the returned repository and job IDs.
3. Poll the job with bounded backoff.
4. Confirm both `job.status == "succeeded"` and `repository.status == "ready"`.
5. Inspect repository statistics and the provider/status endpoint before treating the index as usable.

A `202` response proves only that work was queued. It does not prove acquisition, parsing, embedding, Qdrant persistence, or provider success.

The source archive is moved into its durable repository directory before SQLite mutation. The
repository manifest and ingest job are then inserted in one transaction and read back before the
API returns `202`; there should never be only one of those records after an accepted submission.
Migration 3 enforces at most one job in `queued` or `running` state per repository.

### Reindex

Use reindex after changing the embedding provider/model/dimension, Tree-sitter package,
redaction/rerank/chunk contract, Qdrant collection prefix, or after a recoverable failure. Reindex
uses the immutable archive or extracted snapshot already stored for that repository; it does not
refresh a moving GitHub ref. Moving the repository to `indexing` and inserting the reindex job is
one transaction; existing active work returns a conflict rather than creating a second active job.

The worker builds a new, versioned collection without changing the persisted active collection.
It then atomically stores the new collection name/fingerprint, moves the repository to `ready`, and
succeeds the leased job. Only after that publication does it delete older versions best-effort.

Confirm the new job succeeds, the repository's persisted collection exists, and its fingerprint
matches current settings. Do not mix old vectors with a new embedding dimension. If all reindex
attempts fail, cleanup of the unpublished collection is best-effort and a repository with a last
published collection returns to `ready` only after physical readback. The terminal job/repository
transition is one transaction; without a readable prior collection the repository becomes `failed`.
Do not mistake a failed reindex job for loss of a physically present last-known-good index. The
restored record retains its prior fingerprint and stats: `ready` preserves the old bytes, but
questions still return `409` when the complete current index contract requires a new fingerprint.

### Leases and startup reconciliation

Worker heartbeats renew only `lease_expires_at` and `updated_at` for the current owner. They do not
resubmit stage/progress, while ordinary progress updates remain monotonic and lease-owned. The
worker recovers expired leases when its polling loop starts and before a claim: retryable work is
requeued below the attempt limit, and exhausted work plus its `queued`/`indexing` repository fails.
Stale recovery deliberately does not trust a persisted collection-name string without physical
Qdrant readback. Reindex the failed repository explicitly from its immutable snapshot after
diagnosis.

Every application-container startup with an initialized ingestion service separately reconciles
manifests and storage under per-repository locks. In inline mode it finishes before the worker task
starts; in service mode both API and standalone worker startup may invoke it, with repository locks
serializing overlap:

1. `queued` or `indexing` repositories with no active job are re-enqueued from an existing
   immutable archive/extracted snapshot.
2. Such repositories without a snapshot are marked failed with `snapshot_missing`.
3. A `ready` repository whose persisted physical collection is absent is marked failed with
   `index_missing`.
4. Only staging paths and unreferenced repository directories older than the startup stale-age
   threshold are removed; fresh paths are retained.
5. For `ready` and `failed` repositories, inactive versioned collections are removed while the
   exact `collection_name` persisted in SQLite is preserved.
6. Active work and contended repository locks are skipped.

The reconciliation is idempotent and does not turn SQLite plus Qdrant into one cross-system
transaction. Monitor its safe count log (`jobs_requeued`, `repositories_failed`, `paths_removed`)
after restarts.

### Cancel

Cancellation is valid only for queued jobs and commits before a worker can claim them. A running or
terminal job returns `409`; the service does not forcibly interrupt an active provider call. Verify
the cancelled job state. Cancellation does not change repository state, and a later startup
reconciliation may enqueue replacement work when the repository remains `queued`/`indexing` with a
snapshot and no active job. Delete the repository to stop/remove it; wait for running work to finish
or recover before doing so.

### Delete

Deletion rejects a running job, then synchronously cancels queued work, enumerates/removes every
versioned Qdrant collection bearing the repository UUID marker across configured-prefix histories,
deletes repository files, and verifies every exact collection target plus the repository path. It
then commits the repository-row delete, relies on the SQLite foreign-key cascade for jobs, and
requires a one-row affected count before returning `204`. After a successful response:

1. `GET` the repository and expect `404`;
2. list jobs filtered by repository ID and expect none; and
3. if operating Qdrant directly under an approved maintenance procedure, confirm no collection
   bearing that repository UUID marker remains, including under historical prefixes.

Local deletion does not delete copies in backups, model-provider retention systems, reverse-proxy logs, filesystem snapshots, or operator exports.

## Backup

SQLite, repository archives/extracted snapshots, and Qdrant must be captured as one consistency
set. Encrypt backups, restrict access as tightly as the imported source, and record the persisted
active collection name plus provider/model/index fingerprint with the snapshot.

### Local inline data

Stop API and UI first, then archive the data directory:

```bash
mkdir -p backups
tar --create --gzip \
  --file backups/codebase-intelligence-local-2026-07-17.tar.gz \
  .data
```

Verify the archive can be listed and restore it into a disposable directory before calling the backup tested:

```bash
mkdir -p /tmp/codebase-intelligence-restore-check
tar --extract --gzip \
  --file backups/codebase-intelligence-local-2026-07-17.tar.gz \
  --directory /tmp/codebase-intelligence-restore-check
```

The archive contains source-derived sensitive data; never commit or attach it to ordinary CI artifacts.

### Compose named volumes

Stop all writers before snapshotting both named volumes:

```bash
docker compose stop ui worker api qdrant-probe qdrant
docker volume inspect codebase-intelligence-data
docker volume inspect codebase-intelligence-qdrant
docker volume inspect codebase-intelligence-qdrant-snapshots
```

Use the container platform's volume-snapshot mechanism where possible. For a local Docker engine, export both volumes with a pinned, reviewed helper image and record their shared backup ID. Do not restart any service between the two exports. Restart only after both snapshots are complete:

```bash
docker compose start qdrant qdrant-probe api worker ui
```

Volume metadata alone is not a backup. A Qdrant snapshot without the matching manifest/repository snapshot, or vice versa, is not a verified restore point.

## Restore

Never overwrite the only existing data set during a restore test.

1. Stop API, worker, UI, and Qdrant.
2. Preserve the current data or volume IDs as a rollback point.
3. Restore the matched application and Qdrant backups into new empty paths or new named volumes.
4. Point a disposable deployment at the restored pair.
5. Start Qdrant, then API, then worker/UI.
6. Verify readiness, repository/job state, every `ready` repository's persisted collection and
   fingerprint, a deterministic cited question, and delete behavior on a disposable repository.
7. Promote the restored volumes only after reconciliation; otherwise stop and return to the preserved originals.

For local `.data`, a recoverable swap looks like:

```bash
mv .data .data.pre-restore
tar --extract --gzip \
  --file backups/codebase-intelligence-local-2026-07-17.tar.gz
```

Keep `.data.pre-restore` until the restored application passes readback. Do not run old and restored manifests against the same Qdrant directory.

## Upgrade and rollback

1. Read release notes and diff `pyproject.toml`, `uv.lock`, SQLite schema code, provider models, parser/chunk settings, Docker bases, and Qdrant version.
2. Run `make check`, `docker compose config`, and a clean image build.
3. Back up the complete consistency set and test its restore.
4. Exercise ingestion, question, reindex, cancellation, and deletion on a disposable staging deployment.
5. Stop writers and deploy the image plus compatible Qdrant version.
6. Confirm readiness and status before allowing imports.
7. Reindex repositories whose fingerprint changed.
8. Retain the old image and pre-upgrade snapshot until the acceptance window closes.

There is no promise that a newer SQLite schema, Qdrant storage format, or index fingerprint can be read by an older application. Rollback may require restoring the matched pre-upgrade backup, not merely starting the old image.

## Troubleshooting

### Readiness is degraded

Read the `checks` map and `/api/v1/status` without printing configured secret values.

- `database: false`: verify `/data` exists, is writable by UID 10001, has capacity/inodes, and is on a supported local filesystem.
- `qdrant: false`: verify URL, private-network attachment, Qdrant health, storage permission, and client/server compatibility.
- `embedding: false`: select deterministic mode or provide the selected provider credential to the process that embeds.

Do not mark the service healthy by hiding a failed check.

### Worker runs but jobs do not advance

Inspect the job's status, stage, attempt, lease expiry, and safe error code. Verify API and worker share the same manifest path and Qdrant URL. An expired lease should be requeued until `worker_max_attempts` is exhausted; a repeatedly failing job should end failed rather than loop forever.

There must be at most one queued/running row for a repository. If a legacy database somehow
contains duplicates, confirm migration 3 applied; it cancels duplicate active rows before creating
the partial unique index. Do not remove the index or manually create parallel work.

Container PID health is not proof of polling or progress. Review bounded worker logs by IDs, never by dumping process environments or repository/provider payloads.

### Qdrant storage lock or inconsistent collections

An embedded Qdrant path cannot be shared safely by separate API and worker processes. Stop both,
configure Qdrant Server, and reindex from immutable snapshots. If a manifest says ready but its
persisted collection is missing, or its fingerprint differs from current settings, treat the
repository as inconsistent and reindex; do not select another collection by prefix. Extra inactive
versions may be crash leftovers and are pruned on startup for terminal repositories, but confirm the
persisted active version before any manual cleanup.

### Upload or extraction rejected

Compare the safe error code with configured archive, expansion, path, file, byte, and chunk limits. Do not raise global limits before verifying the archive's provenance and inspecting its structure in an isolated process. The correct action for a traversal, encrypted entry, symlink, device, or suspicious expansion is rejection.

### Provider unavailable or rate-limited

Confirm the selected provider/model and its runtime `ready` flag in `/api/v1/status`; check the
provider control plane through an approved credential-safe channel. Answer-provider readiness is
separate from `/health/ready`, so an OpenAI synthesis failure need not make embedding/Qdrant
readiness red. Do not log response bodies or retry without a cap. Deterministic/extractive mode can
prove application flow, but it is not equivalent paid-provider quality proof.

### Answer has no citations

An empty citation list is valid for insufficient evidence. Confirm the repository is ready, the
question names repository concepts, `top_k` is within bounds, repository statistics show chunks,
the persisted collection exists, and the persisted fingerprint matches current settings. The query
path uses that exact collection and returns `409` for a missing/stale publication; it does not widen
to another version or repository.

### Delete returns an error

Treat deletion as incomplete. Record the request, repository, and safe error IDs; stop new work for that repository; determine whether vector, filesystem, jobs, or manifest cleanup failed; and retry only after reading current state. Never convert a partial cleanup into `204` manually.

## Local validation boundary

`make coverage` enforces branch coverage for the application package except
`src/codebase_intelligence/ui/app.py`. Streamlit AppTest runs that script in an untraced
script-runner context, so including it would produce a misleading denominator. The UI is instead a
separate gate: 15 Streamlit AppTest/API-client tests exercise empty, indexing, failed, ready/chat,
delete, error, token, upload, and client-contract states.

The final integrated localhost proof used the real FastAPI app and inline worker with deterministic
embeddings, extractive answers, SQLite, and embedded Qdrant. The native browser file chooser was
unsupported, so the fixture ZIP entered through the real API rather than the browser control. The
worker indexed 6 files/12 chunks with 1 redaction; desktop browser questions rendered cited auth and
payment paths/symbols/lines; real reindex visibly traversed queued/indexing to `ready`; and the
390×844 mobile layout rendered the cited payment answer. Readiness reported
`database/embedding/qdrant/worker: true`.

The final clean desktop run had zero console warnings/errors. A mobile reindex rerender produced one
user-invisible upstream Streamlit wavesurfer `Container not found` console error, so do not claim a
console-clean mobile run. This proof is not browser-driven file selection, a live Voyage/OpenAI or
private-GitHub check, a Docker-runtime check, or hosted/production evidence.

## Production readiness checklist

The included Compose file is local infrastructure, not production proof. Before deployment beyond a trusted host, require evidence for:

- authenticated TLS ingress and identity-aware authorization;
- exact CORS and trusted-host policy;
- upload, request, concurrency, and distributed rate limits;
- secret-manager injection and rotation;
- provider data-processing/retention approval;
- egress allowlists and private Qdrant networking;
- encrypted durable storage and backups;
- single-host SQLite suitability or an approved persistence redesign;
- image digest pinning, SBOM, signing, vulnerability scanning, and patch cadence;
- centralized redacted logs, metrics, alerts, queue-age and deletion monitoring;
- tested backup, restore, upgrade, rollback, and disaster recovery;
- per-tenant storage/auth isolation if more than one trust domain is served; and
- separate hosted smoke, load, failure, security, and deletion-readback proof.

Record local, container, hosted-dev, provider, and production evidence separately. A green deterministic test or localhost screenshot does not prove any unexercised layer.
