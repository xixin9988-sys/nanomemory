/**
 * nanomemory-dsh-plugin — mount the local NanoMemory MCP server into DeepSeek Harness.
 *
 * The plugin itself implements no transport: it builds a stdio MCP configuration and hands it
 * to DSH's in-box `@deepseek-ai/dsh-mcp-client` bridge, exactly like other local MCP plugins.
 * Tool registration, naming (`mcp__nanomemory__*`), reconnect and lifecycle stay inside DSH.
 *
 * Why a Python child process instead of JS memory code: NanoMemory's guarantees (sidecar
 * transaction journal, cross-process locks, crash recovery, secret scanning, quarantine) live
 * in the Python engine and its tests. Duplicating them in JavaScript would create two
 * implementations of the same invariants.
 *
 * Configuration (all optional; the profile installer writes explicit paths):
 *   - `pythonPath`   Python executable (default: env NANOMEMORY_PYTHON or "python")
 *   - `projectRoot`  directory containing the `nanomemory` package (default: env NANOMEMORY_ROOT)
 *   - `storageRoot`  memory storage root passed to the child (default: NANOMEMORY_STORAGE_ROOT)
 *   - `serverName`   tool namespace (default "nanomemory", `[A-Za-z0-9_-]{1,32}`)
 *   - `provider`     optional provider override for the child (`mock` / `openai_compatible`)
 *   - `toolCallTimeoutMs` (default 120000), `failOnStartupError` (default false)
 */
import path from 'node:path';

export const name = 'nanomemory';

/** The bridge registers tools through `ctx.tools`. */
export const inject = ['tools'];

export const DEFAULT_SERVER_NAME = 'nanomemory';
export const DEFAULT_TOOL_TIMEOUT_MS = 120000;

const SERVER_NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;

/**
 * Build the stdio MCP configuration for `@deepseek-ai/dsh-mcp-client`.
 *
 * Exported separately so it can be unit-tested without a running DSH host.
 *
 * @param {Record<string, unknown>} config
 * @param {NodeJS.ProcessEnv} env
 * @returns {Record<string, unknown>}
 */
export function bridgeConfig(config = {}, env = process.env) {
  const serverName = String(config.serverName ?? DEFAULT_SERVER_NAME);
  if (!SERVER_NAME_RE.test(serverName)) {
    throw new Error(
      `[nanomemory] serverName must match [A-Za-z0-9_-]{1,32}; got ${JSON.stringify(serverName)}`,
    );
  }

  const pythonPath = String(config.pythonPath ?? env.NANOMEMORY_PYTHON ?? 'python').trim();
  if (!pythonPath) throw new Error('[nanomemory] pythonPath must not be empty');

  const projectRoot = String(config.projectRoot ?? env.NANOMEMORY_ROOT ?? '').trim();
  const extraArgs = Array.isArray(config.pythonArgs) ? config.pythonArgs.map(String) : [];

  const childEnv = { ...env };
  if (projectRoot) {
    // Make `python -m nanomemory.mcp_server` importable without installing the package.
    childEnv.PYTHONPATH = env.PYTHONPATH
      ? `${projectRoot}${path.delimiter}${env.PYTHONPATH}`
      : projectRoot;
    childEnv.PYTHONUNBUFFERED = '1';
  }
  const storageRoot = String(config.storageRoot ?? '').trim();
  if (storageRoot) childEnv.NANOMEMORY_STORAGE_ROOT = storageRoot;
  if (config.provider) childEnv.NANOMEMORY_PROVIDER = String(config.provider);

  return {
    serverName,
    transport: 'stdio',
    command: pythonPath,
    args: [...extraArgs, '-m', 'nanomemory.mcp_server'],
    env: childEnv,
    toolCallTimeoutMs: Number(config.toolCallTimeoutMs ?? DEFAULT_TOOL_TIMEOUT_MS),
    failOnStartupError: Boolean(config.failOnStartupError ?? false),
  };
}

/**
 * Cordis entry point: mount the MCP bridge.
 *
 * `await connection` matters: `ctx.plugin()` returns a thenable fiber, and returning it instead
 * of awaiting makes Cordis treat the fiber as a disposal callback.
 *
 * @param {any} ctx Cordis context
 * @param {Record<string, unknown>} config
 */
export async function apply(ctx, config = {}) {
  const bridge = await import('@deepseek-ai/dsh-mcp-client');
  const bridgeConfigValue = bridgeConfig(config);
  const connection = ctx.plugin(bridge, bridgeConfigValue);
  await connection;
  ctx.logger?.info?.(
    '[nanomemory] MCP server started (tools appear as mcp__%s__*) using %s',
    bridgeConfigValue.serverName,
    bridgeConfigValue.command,
  );
}
