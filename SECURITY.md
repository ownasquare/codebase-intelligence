# Security Policy

Codebase Intelligence processes adversarial archives, source code, and questions. Reports about
archive extraction, URL validation, authorization, repository isolation, secret handling, prompt
injection, deletion, or dependencies are welcome.

## Supported versions

| Version | Security fixes |
|---|---|
| `0.3.x` | Yes |
| `0.2.x` and earlier | Upgrade required |
| Unreleased snapshots | No guarantee |

This project is beta software. Use the latest maintained release and review its lockfile and
deployment configuration before handling private source.

## Report a vulnerability

Do not open a public issue with exploit details, credentials, private source, or provider
responses.

On GitHub, open this repository's **Security** tab and choose **Report a vulnerability**. That
creates a private report when private vulnerability reporting is enabled. If that option is not
available, open a minimal issue asking the maintainers for a private reporting channel; do not
include security details in the issue.

Include:

- the affected version or revision and deployment mode;
- attack preconditions and the crossed trust boundary;
- a minimal synthetic reproduction with no real secrets or proprietary source;
- expected and observed behavior;
- likely impact; and
- any safe mitigation already tested.

Maintainers aim to acknowledge a report within three business days. Validation, severity,
remediation, and disclosure timing depend on impact and reproduction. Updates will stay in the
private report until coordinated disclosure is safe.

## Credential handling

- Keep provider and API credentials out of source, images, URLs, query strings, job payloads, logs,
  screenshots, fixtures, and vector metadata.
- Supply private GitHub tokens only through the request-scoped `X-GitHub-Token` header.
- Use short-lived, least-privilege credentials and rotate suspected exposures.
- Never put a production credential in a vulnerability report.
- Treat repository text, model output, remote errors, filenames, refs, and URLs as untrusted.

## Deployment expectations

The application is safe-shaped for local use, not a turnkey public multi-tenant perimeter. An
operator exposing it beyond loopback is responsible for:

1. authenticated TLS ingress and secure API-key distribution;
2. tenant authorization and storage isolation;
3. edge request, upload, concurrency, and rate limits;
4. restricted outbound network access;
5. managed secrets and log redaction;
6. durable SQLite and Qdrant backups with tested restore;
7. non-root process and container isolation;
8. dependency and base-image monitoring; and
9. appropriate deletion, retention, audit, and incident response.

Readiness proves configured local dependencies, not separately supervised worker progress,
paid-provider quality, backups, tenant isolation, or production readiness.

## Known limitations

The pipeline rejects common archive traversal, symlink, device, encryption, expansion, and SSRF
techniques; filters sensitive file classes; and performs best-effort text redaction. It is not
malware scanning, source classification, or complete secret detection.

Prompts label repository context as untrusted and validate generated citation IDs. Model behavior
remains probabilistic. Do not treat an answer as a security decision, code-review approval, or
proof that omitted source is safe.

See the [threat model](docs/security/threat-model.md) for trust boundaries, controls, and residual
risks.
