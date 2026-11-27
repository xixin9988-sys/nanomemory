# ADR-0002 — Embedding profiles define vector compatibility

- **Status:** Accepted; staged implementation
- **Current state:** each row stores `embedding_model` and `embedding_dim`; retrieval filters on
  the active model. A profile id column and shadow re-embedding are the next M1 step (NM-04).
- **Related:** ADR-0001, PRD §7

## Context

An embedding is a function of (model, dimension, normalisation, preprocessing). Vectors from
different functions are not comparable. If a deployment changes the model and keeps old vectors,
cosine similarity becomes noise that looks like a ranking.

## Decision

1. A deployment has exactly **one active embedding profile** at a time:

   ~~~json
   {
     "profile_id": "prod-2026-01:text-embedding-3-small:1536:v1",
     "provider": "openai_compatible",
     "model": "text-embedding-3-small",
     "dimension": 1536,
     "normalization": "l2",
     "preprocessing_version": "v1"
   }
   ~~~

2. Writes, searches and traces record the profile id. A search only compares vectors from the
   active profile.
3. A dimension mismatch is a hard error (422), never a truncate/pad.
4. Changing the profile is a migration, not a config flip:
   - build the new vectors into a shadow column/table (no serving change);
   - verify count parity and a sample of retrieval quality on the frozen eval set;
   - switch the active profile atomically;
   - keep the old profile's data until the rollback window closes.
5. Until the profile column lands, the model+dimension filter is the compatibility guard, and
   `embedding_model_changed` is surfaced instead of silently returning nothing.

## Consequences

- Re-embedding costs money and time; the migration is explicit and can be scheduled.
- Mixed-vector retrieval bugs become impossible by construction once profile filtering is in
  place.
- The eval report must name the profile it was produced with; a score without a profile is not
  comparable.
