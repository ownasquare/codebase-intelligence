# Local Startup Follow-Up

Date: 2026-07-17

Repository: `/Users/fortunevieyra/Documents/Github/ai-projects/codebase-intelligence`

## Outcome

The credential-free local application is running and ready for use at [http://127.0.0.1:8501](http://127.0.0.1:8501). FastAPI is listening on port 8000, Streamlit is listening on port 8501, and the in-app browser has been left open on the ready workspace.

An ignored `.env` was created with deterministic embeddings, extractive answers, embedded Qdrant, and the inline worker. It contains no provider credential. `make sync` revalidated the lock and audited the installed dependency set without changes.

## Runtime Readback

- API process observed at closeout: PID 34002 on `127.0.0.1:8000`.
- UI process observed at closeout: PID 34001 on `127.0.0.1:8501`.
- Readiness: database, embedding, Qdrant, and worker all returned `true`.
- Status: deterministic/hash embeddings, extractive answers, embedded Qdrant, inline worker, development environment.
- Streamlit health endpoint returned `ok`.
- `.env` and `.data/` are ignored; the tracked Git worktree remained clean before this record was added.

Process IDs are observational and may change after a restart. Recheck the endpoints rather than treating these identifiers as durable ownership proof.

## Ready Sample

The bundled synthetic fixture was archived to `/tmp/codebase-intelligence-sample-20260717.zip` and submitted through the actual upload API.

- Repository: `4847bfb6-399a-45d6-94bd-3519cc201ff6`
- Job: `37df78cd-ed4b-43ea-8d21-20a8fe5ec672`
- Job result: `succeeded`, stage `complete`, progress 100%, attempt 1
- Indexed state: 6 files, 12 chunks, 2.2 KB, 1 redaction, 4 Tree-sitter parsed files, 2 fallback-parsed files

The rendered UI automatically discovered the ready repository. The sample question “Where is the authentication logic?” returned an extractive answer with repository-scoped citations including `src/auth.py`, `src/gateway.py`, and `src/payments.py`.

## Browser QA

The in-app Browser plugin was available and used. The checked flow was:

`app loads -> API-connected workspace renders -> ZIP input tab responds -> ready sample appears -> sample question renders cited answer`

| Check | Result |
| --- | --- |
| Page identity | Passed: `http://127.0.0.1:8501/`, title `Codebase Intelligence`. |
| Meaningful content | Passed: product heading, provider status, repository input, and ready repository were present. |
| Framework/error overlay | Passed: none present. |
| Console health | Passed: zero warnings or errors at final readback. |
| Interaction | Passed: ZIP tab selection and authentication sample question both changed the rendered state correctly. |
| Screenshot | Passed: current cited-answer viewport was emitted in the completion chat. |

## Operations

If the local processes are no longer running, restart them from the repository root in separate terminals:

```bash
make api
make ui
```

Verify:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/health/ready
curl --fail --silent --show-error http://127.0.0.1:8501/_stcore/health
```

Stop each foreground process with `Ctrl-C`. The current `.data/` state preserves the sample repository between normal local restarts.

## Proof Boundary

This follow-up proves the credential-free local runtime and rendered browser flow. It does not add live Voyage/OpenAI, private-GitHub, external-Qdrant, hosted-development, or production proof.
