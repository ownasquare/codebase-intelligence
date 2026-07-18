# Security Policy

Codebase Intelligence processes adversarial archives, source code, and natural-language questions. Security reports are welcome, especially for archive extraction, URL validation, authorization, repository isolation, secret handling, prompt injection, deletion, and dependency behavior.

## Supported versions

| Version | Security fixes |
|---|---|
| `0.1.x` | Yes |
| Earlier or unreleased snapshots | No guarantee |

Until a stable release exists, use the latest maintained revision and review its lockfile and deployment configuration before handling private source.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, private repository content, or provider responses. If this project is hosted on a forge that supports private security advisories, use that channel. Otherwise contact the maintainer through an established private channel and include only the minimum information needed to reproduce the issue.

Useful reports include:

- affected revision and deployment mode;
- attack preconditions and trust boundary;
- a minimal synthetic reproduction without real secrets or proprietary source;
- expected and observed behavior;
- impact, including whether data crossed repository or tenant boundaries; and
- any safe mitigation already tested.

Maintainers should acknowledge a report privately, reproduce it without widening exposure, assign severity, prepare a regression test and fix, audit adjacent boundaries, and coordinate disclosure after affected users have a practical remediation.

## Credential handling

- Provider and API credentials belong in a process secret store, not source, images, URLs, query strings, job payloads, logs, screenshots, fixtures, or vector metadata.
- A private GitHub token is accepted only in the request-scoped `X-GitHub-Token` header. It must not be persisted or forwarded to the worker.
- Use least-privilege, short-lived credentials and rotate them after any suspected exposure.
- Never submit a production credential in a vulnerability report.
- Treat repository text, model output, remote error bodies, filenames, refs, and URLs as untrusted and potentially credential-bearing.

## Deployment expectations

The application is safe-shaped for local use, not a turnkey public multi-tenant perimeter. An operator exposing it beyond loopback is responsible for:

1. authenticated TLS ingress and secure API-key distribution;
2. per-tenant authorization and storage isolation when multiple trust domains exist;
3. request, upload, concurrency, and rate limits at the edge;
4. restricted outbound network access to the required GitHub and provider endpoints;
5. a managed secret store and log redaction policy;
6. durable SQLite and Qdrant backups with tested restoration;
7. filesystem, process, and container isolation with non-root execution;
8. vulnerability monitoring for the lockfile and container bases; and
9. deletion, retention, audit, and incident-response procedures appropriate to the source being indexed.

Liveness is not readiness. Readiness proves the complete SQLite migration set, operational embedding
initialization, and a Qdrant health call; when the API owns an inline worker, it also requires that
task to remain running. It does not prove a separately supervised worker, job progress, optional
answer synthesis, paid-provider quality/quota, backups, or deletion controls. Inspect the protected
provider-status and job endpoints separately.

## Scope and known limitations

The ingestion pipeline rejects common path traversal, symlink, device, encryption, expansion, and SSRF techniques; filters sensitive file classes; and performs best-effort text redaction. These controls do not constitute malware scanning, source-code classification, complete secret detection, or a guarantee that an external model provider cannot receive sensitive source text.

The grounded prompt labels repository context as untrusted and citation IDs are validated. Model behavior remains probabilistic. Do not treat an answer as a security decision, code review approval, or proof that omitted source is safe.

Built-in Swagger, ReDoc, and the default public OpenAPI path are disabled. The schema is available
only through `/api/v1/openapi.json`, which is protected whenever the shared application API key is
configured. This reduces accidental contract disclosure but does not replace identity-aware
authorization.

See the detailed [threat model](docs/security/threat-model.md) for trust boundaries, mitigations, and residual risks.
