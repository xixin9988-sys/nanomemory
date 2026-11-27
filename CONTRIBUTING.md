# Contributing to NanoMemory

Thanks for considering a contribution. NanoMemory is a memory engine where data integrity, provenance, access boundaries and honest retrieval behaviour matter as much as feature completeness.

## Before opening an issue

Search existing issues and read:
- README.md for current supported behaviour;
- docs/ARCHITECTURE.md for the current implementation boundaries;
- docs/SECURITY.md and SECURITY.md for security limitations;
- docs/product/PRODUCT_BRIEF_VECTOR_MEMORY_V1.md and docs/adr/ for the proposed vector-memory direction.

The vector-memory v1 documents are proposals until approved and implemented. Please do not treat future interfaces as available APIs.

## Development workflow

1. Open or select an issue and agree on scope before a broad refactor.
2. Create a focused branch from main (or the explicitly named integration branch).
3. Keep one responsibility per pull request.
4. Add or update tests with the implementation.
5. Run the relevant test suite and lint/format checks locally.
6. Describe compatibility impact, migration requirements and security implications in the PR.
7. Do not include real user memory, API keys or private deployment data in fixtures or screenshots.

## Quality requirements

A pull request should include:
- a short problem statement and explicit non-goals;
- public API and data-contract changes, if any;
- unit and integration tests;
- Given / When / Then acceptance criteria for user-visible behaviour;
- loading, empty, error, permission-denied and recovery behaviour where applicable;
- migration and rollback notes for persisted data;
- performance impact for retrieval, storage or background jobs;
- documentation updates;
- evidence for any performance, security or accuracy claim.

For vector retrieval changes, include the dataset revision, embedding profile, tokenizer and configuration used for evaluation. Never report mock context coverage as real model answer quality.

## Checks

Run the commands supported by the current checkout:
- python -m pytest
- ruff check .
- ruff format --check .

For future PostgreSQL/pgvector changes, the pull request must also pass the pinned PostgreSQL + pgvector integration suite and the retrieval/security evaluation gates.

## Pull request review

Use a clear title, link the related issue and explain how to reproduce the change. Mark a PR as draft while its contract or migration design is still under discussion.

Maintainer response target: **proposed target of three business days for an initial triage response**, subject to maintainer approval and availability. This is not a guaranteed support SLA.

## Good first issues

A good first issue should be explicitly labelled good first issue, have a bounded scope, name the expected tests and avoid changing persisted schemas or security boundaries without maintainer review.