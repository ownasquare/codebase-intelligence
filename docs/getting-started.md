# Getting Started

This guide takes you from a new checkout to one cited answer. No model-provider account is required.

## What you need

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Make
- Git

Docker is optional. Windows users should use WSL for the Make-based local workflow.

## Start the app

From the project checkout:

```bash
make demo
```

The command installs the locked dependencies, starts FastAPI and Streamlit in one terminal, and
uses deterministic embeddings with extractive answers. Open <http://127.0.0.1:8501>.

Keep the terminal open. Press `Ctrl+C` once to stop both processes.

## Complete a first question

You can use any public GitHub repository or a ZIP you are allowed to inspect. For a predictable
first run, create a ZIP from the bundled synthetic fixture:

```bash
python -m zipfile -c /tmp/codebase-intelligence-sample.zip tests/fixtures/sample_repo
```

In the app:

1. In **Add your first repository**, choose a source.
2. Choose **ZIP upload** and upload `/tmp/codebase-intelligence-sample.zip`.
3. Wait for the repository state to become **Ready**.
4. In **Ask**, enter: `Where is the authentication logic?`
5. Open one of the cited findings. The app moves to **Source** and shows its indexed file and line
   range.

In credential-free extractive mode, the response is a ranked set of cited locations rather than a
generated narrative. That is expected. A successful first run proves the local workflow, not
paid-provider quality or production readiness.

## Add a GitHub repository

Choose the GitHub option and enter a canonical URL:

```text
https://github.com/owner/repository
```

An optional ref may be a branch, tag, or commit. Only canonical GitHub repository URLs are accepted;
arbitrary archive URLs are not.

For a private repository, provide a short-lived, least-privilege token in the interface. The token
is used only while the API downloads the archive. It is not stored in the job, manifest, vector
payload, or worker configuration. Do not place a token in a URL or commit it to `.env`.

## Optional provider setup

The default configuration requires no credentials. Copy the example only when you want persistent
settings:

```bash
cp .env.example .env
```

For Voyage code embeddings with extractive answers, change:

```dotenv
CODEBASE_INTEL_EMBEDDING_PROVIDER=voyage
CODEBASE_INTEL_ANSWER_PROVIDER=extractive
VOYAGE_API_KEY=replace-in-your-private-secret-store
```

For OpenAI embeddings and synthesized answers, change:

```dotenv
CODEBASE_INTEL_EMBEDDING_PROVIDER=openai
CODEBASE_INTEL_ANSWER_PROVIDER=openai
OPENAI_API_KEY=replace-in-your-private-secret-store
```

Restart the app after changing settings. Reindex an existing repository whenever the embedding
provider, model, dimension, parser contract, redaction contract, or chunk settings change.

Source-derived text is sent to the selected embedding provider and, when enabled, the answer
provider. Confirm that this is allowed before indexing private code.

## Run with Docker

Docker Compose starts separate API, worker, UI, and Qdrant services with credential-free defaults:

```bash
docker compose config --quiet
make compose-up
```

Open <http://127.0.0.1:8501>. Only the interface is published to the host. Stop the services without
deleting stored repositories:

```bash
make compose-down
```

Named volumes keep application and Qdrant data. Removing volumes is a separate destructive action.

## Common problems

### The page does not open

Wait for both local services to report that they started. Confirm ports `8000` and `8501` are
available, then retry `make demo`.

### The interface says the service is unavailable

Check <http://127.0.0.1:8000/api/v1/health/ready>. Restart the demo if the API terminal stopped.
When using separate commands, set
`CODEBASE_INTEL_API_BASE_URL=http://127.0.0.1:8000` for the interface.

### A repository stays in indexing

Open the repository status and read the current job stage. Provider credentials, quota, archive
limits, and Qdrant availability are common causes. Credential-free mode should show
`deterministic` embeddings and `extractive` answers.

### An upload is rejected

Use a normal `.zip` with relative files. Encrypted archives, symlinks, path traversal, excessive
nesting, oversized content, and excluded sensitive file types are rejected by design.

### Results are weak in the local demo

Deterministic embeddings are a setup and development path, not a semantic-quality benchmark.
Try a more specific question with a path or symbol term, or opt in to Voyage/OpenAI after reviewing
the data policy.

For operational diagnosis, see the [runbook](operations/runbook.md). For usage questions and issue
routing, see [Support](../SUPPORT.md).
