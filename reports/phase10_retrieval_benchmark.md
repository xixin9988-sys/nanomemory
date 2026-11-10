# Retrieval benchmark (P0)

- records in store: 60 across 10 cases
- k = 5

| metric | value |
| --- | --- |
| Fact Recall@5 (hybrid v2) | 60.71% |
| Fact Recall@5 (legacy keyword) | 60.71% |
| Precision@5 | 28.00% |
| Provenance coverage | 100.00% |
| Temporal update accuracy | 100% |
| Conflict detection accuracy | 100% |

Per case:

| case | required | hit | recall@k |
| --- | --- | --- | --- |
| case-001 | 2 | 2 | 1.00 |
| case-002 | 3 | 2 | 0.67 |
| case-003 | 3 | 2 | 0.67 |
| case-004 | 3 | 2 | 0.67 |
| case-005 | 3 | 2 | 0.67 |
| case-006 | 3 | 1 | 0.33 |
| case-007 | 3 | 1 | 0.33 |
| case-008 | 3 | 3 | 1.00 |
| case-009 | 3 | 0 | 0.00 |
| case-010 | 2 | 2 | 1.00 |
