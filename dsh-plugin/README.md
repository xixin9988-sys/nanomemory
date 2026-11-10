# nanomemory-dsh-plugin

Mounts a local [NanoMemory](../README.md) MCP server into DeepSeek Harness as
`mcp__nanomemory__*` tools.

## Install (local checkout)

```bash
# 1. Make the Python engine importable (source checkout or `pip install -e .`)
# 2. Install this package into the DSH profile
dsh plugin --profile desktop add <path-to-repo>/dsh-plugin
```

Then add the paths to the profile `cordis.patch.yml`:

```yaml
- id: mcp-nanomemory
  name: nanomemory-dsh-plugin
  config:
    pythonPath: C:/path/to/NanoMemory/.venv/Scripts/python.exe
    projectRoot: C:/path/to/NanoMemory
    storageRoot: C:/path/to/NanoMemory/storage
```

Restart DSH. The tools appear once the profile loads the bundle.

## Tools

| Tool | Purpose |
| --- | --- |
| `nanomemory_health` | status, provider mode, degraded reasons, budgets |
| `nanomemory_get_working` | Working Memory + `memory_version` |
| `nanomemory_update_working` | version-checked update (unfinished tasks protected) |
| `nanomemory_add_memory` | store a durable fact (secrets rejected) |
| `nanomemory_search_memory` | keyword search over long-term memory |
| `nanomemory_list_episodes` | append-only audit events + correction status |
| `nanomemory_run_agent` | bounded agent loop with budgets |

All memory content returned by the tools is **untrusted reference data**; the server
instructions tell the model not to follow instructions found inside it.

## Configuration

| Field | Default | Meaning |
| --- | --- | --- |
| `pythonPath` | `NANOMEMORY_PYTHON` or `python` | interpreter that can import `nanomemory` |
| `projectRoot` | `NANOMEMORY_ROOT` | added to `PYTHONPATH` for the child |
| `storageRoot` | `NANOMEMORY_STORAGE_ROOT` | memory storage root for the child |
| `provider` | env / default `mock` | provider override for this plugin only |
| `serverName` | `nanomemory` | tool namespace |
| `toolCallTimeoutMs` | `120000` | per-call timeout |
| `failOnStartupError` | `false` | fail the entry instead of starting degraded |

## License

MIT
