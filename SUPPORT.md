# Support

Codebase Intelligence is a community beta. The clearest report is short, reproducible, and free of
private code or credentials.

## Choose the right place

| Need | Where to go |
|---|---|
| Setup or usage question | Check [Getting started](docs/getting-started.md), then open a question in GitHub Issues |
| Reproducible defect | Use the **Bug report** issue form |
| Product or extension idea | Use the **Feature request** issue form |
| Security vulnerability | Follow the private process in [SECURITY.md](SECURITY.md) |
| Contribution help | Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [extension guide](docs/development/extending.md) |

Do not place tokens, private repository names, proprietary source, provider payloads, or resolved
environment configuration in an issue.

## Before opening an issue

1. Confirm the problem still occurs on a supported version.
2. Search existing issues.
3. Try the credential-free `make demo` path when relevant.
4. Reduce the problem to a synthetic or public repository.
5. Record the application version, operating system, local or Docker mode, and exact steps.

Logs should be trimmed to the smallest useful section and reviewed for sensitive text.

## Current support boundary

The maintained path is Python 3.12 with the locked dependencies or the repository's Docker Compose
topology. Deterministic/extractive mode is supported for local setup and testing. Voyage AI,
OpenAI, private GitHub access, and production-shaped deployments depend on external accounts,
policies, networking, and quotas.

This beta does not promise hosted service availability, multi-tenant identity, or production
operations. Maintainers will prioritize security, data isolation, reproducible crashes, broken
installation, and failures in the add → Ask → Source workflow.

## Useful health checks

With the local API running:

```bash
curl --fail-with-body http://127.0.0.1:8000/api/v1/health/live
curl --fail-with-body http://127.0.0.1:8000/api/v1/health/ready
```

For deeper diagnosis, use the [operations runbook](docs/operations/runbook.md).
