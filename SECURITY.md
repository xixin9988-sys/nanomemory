# Security Policy

## Supported deployment status

The current main branch is documented as a local single-user MVP. The vector-memory v1 branch is a proposal and does not yet provide a production-ready multi-tenant or internet-facing service. Do not expose an unreviewed build directly to the public internet or treat caller-supplied user IDs as authentication.

## Reporting a vulnerability

Please do not publish exploitable vulnerability details in a public issue.

Use GitHub's private Security Advisory reporting flow for this repository when it is enabled. If private reporting is unavailable, contact the repository owner through the GitHub profile and request a private disclosure channel. Include:
- affected version or commit;
- deployment mode and configuration;
- impact and trust boundary;
- minimal reproduction steps;
- logs or traces with secrets and personal data removed.

Do not include API keys, access tokens, private memory content or personal data in a report.

## Security boundaries for the planned vector release

The proposed design requires:
- trusted server-side authentication context; a request body cannot establish identity;
- tenant and security-domain isolation before approximate nearest-neighbour retrieval;
- PostgreSQL row-level security as defence in depth, not as a substitute for physical ANN partition isolation;
- no shared global HNSW index across mutually isolated security domains;
- exact filtered retrieval or denial when a fine-grained ACL cannot be aligned to an ANN security domain;
- server-side embedding/model credentials; no provider secrets in browser code;
- provenance and lifecycle validation before model-derived memories become active;
- structured audit events, bounded retention and redaction;
- backup verification and a tested restore process.

## Secrets

Never commit credentials, private keys, database URLs containing passwords, production data or unredacted memory exports. Rotate any credential accidentally committed and treat it as compromised even after deleting the commit.

## Scope and limitations

The controls listed in the vector-memory product brief and ADR are proposed requirements until code, tests and deployment evidence exist. A document in this repository is not proof that a control is implemented.