# Security model

This is a **local single-user MVP**. It is not hardened for multi-tenant or internet-facing
deployment, and the documentation says so on purpose.

## 1. Trust boundaries

| Input | Trust |
| --- | --- |
| Environment variables / `.env` | operator-controlled configuration |
| HTTP request bodies, headers | untrusted |
| Model output | untrusted |
| Memory content (Working, Long-term, Episodic) | **untrusted reference data** |
| Tool results | untrusted |
| Files read by the file tool | untrusted |

Memory content is delivered to the model in a separate `<untrusted_memory>` block inside the
*user* message. The system message states explicitly that the block is data, not instructions,
and that it must not change tool permissions, request secrets or trigger tool execution.
Escaping prevents forged closing tags. Tool permissions are enforced by the Tool Registry in
code, never by the payload.

## 2. Secrets

- Keys only come from `NANOMEMORY_API_KEY` / `NANOMEMORY_ADMIN_TOKEN`; they are stored as
  `SecretStr`, never serialised by `/health`, never logged, never written to storage.
- The validator scans every write for OpenAI/Anthropic/GitHub/AWS/PEM credential patterns and
  for generic high-entropy candidates (Shannon entropy > 4.5 bits/char).
- Default policy is **reject** (`422 secret_detected`). Redaction is opt-in and records the
  hit types, never the secret.
- Allowlists (hashes of length 32/40/64, JWT, image data URIs, paths/URLs) apply **only** to
  the generic entropy rule; they can never suppress an explicit credential pattern.
- Logs are redacted at the handler after formatting, which covers messages, structured
  fields, exception text and third-party loggers. `request_id` is preserved.

## 3. Filesystem

- Every user- or model-influenced path is resolved and must stay inside the storage root.
- Controlled directories reject symlinks **and NTFS junctions** (reparse points), including
  the health-check directory.
- The file-reading tool is limited to an explicit root, an extension allowlist and a maximum
  size; it never accepts absolute paths from the model.
- Writes use a same-directory temp file, `fsync`, hash verification and `os.replace`.
- Orphan temp cleanup only touches files whose names match NanoMemory's mandated pattern,
  inside the storage root, older than the configured age and unreferenced by any journal.

Residual risks:

- A determined local attacker with write access to the storage directory can modify memory
  files. Hash checks detect a body/meta mismatch and move the session to
  `recovery_required`, but they cannot prevent local tampering.
- Windows does not expose directory `fsync`; a power loss immediately after a rename can, in
  principle, lose the rename. Startup recovery still resolves to a complete old or new state,
  and the affected tests are marked with that platform limitation.
- Paths longer than ~260 characters fail on Windows without long-path support. Keep the
  project/storage path short.

## 4. Destructive operations

- Backup restore, permanent delete, quarantine acknowledgement and export require
  `NANOMEMORY_ADMIN_TOKEN`; when it is unset they are **disabled** (`503`), not open.
- Comparison is constant-time (`hmac.compare_digest`); failures do not reveal whether a token
  exists.
- Restore additionally requires a confirmation string equal to the backup id, verifies the
  manifest and every hash, writes a pre-restore snapshot, and requires `force=true` to move a
  session version backwards.
- The dashboard asks for the token on every destructive action, keeps it only for that
  server-side call, deletes it afterwards and rotates the widget key. `st.session_state` is
  explicitly **not** treated as authentication or as guaranteed to be cleared by a browser
  refresh.

## 5. HTTP surface

- Default bind is localhost; the app itself does not open network listeners. The Docker image
  listens on `0.0.0.0` *inside* the container and compose publishes it as
  `127.0.0.1:8000`.
- CORS is closed by default; enabling it requires an explicit origin allowlist.
- Write requests require `Content-Length` and are size limited.
- Rate limiting is per client (remote address). `X-Forwarded-For` is **not** trusted.
- Because the limiter is in-memory, a multi-worker deployment would not have a global limit;
  `/health` reports `rate_limit_not_shared` in that configuration instead of claiming
  otherwise. Multi-worker mode is not supported in v1.
- No stack traces, filesystem paths or secrets are returned in error bodies.

## 6. Docker

- Non-root user (`uid/gid 10001`), dedicated `/data` volume.
- Healthcheck uses the Python standard library (`scripts/health_probe.py`) because the slim
  image has no `curl`.
- No credentials are baked into the image; they are supplied at runtime.

Host volume permission note: the container user must be able to write `/data`. If a host
directory is bind-mounted, either `chown -R 10001:10001 <host-dir>` or run with a matching
`--user`. Named volumes created by compose inherit the correct ownership.

## 7. Reporting

This is a local project without a disclosure process. If you deploy it beyond localhost, put
an authenticating reverse proxy in front of it, keep `NANOMEMORY_ADMIN_TOKEN` set, and read
this document first.
