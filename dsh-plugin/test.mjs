// Unit tests for the DSH plugin adapter (no DSH host required).
// Run with: node --test dsh-plugin/test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';
import { bridgeConfig, DEFAULT_SERVER_NAME, DEFAULT_TOOL_TIMEOUT_MS, inject, name } from './index.mjs';

test('exports the cordis identity and tool injection', () => {
  assert.equal(name, 'nanomemory');
  assert.deepEqual(inject, ['tools']);
  assert.equal(DEFAULT_SERVER_NAME, 'nanomemory');
});

test('builds a stdio MCP config for the python module', () => {
  const config = bridgeConfig(
    { pythonPath: 'C:/python/python.exe', projectRoot: 'C:/repo', storageRoot: 'C:/repo/storage' },
    { PATH: 'x' },
  );
  assert.equal(config.transport, 'stdio');
  assert.equal(config.command, 'C:/python/python.exe');
  assert.deepEqual(config.args, ['-m', 'nanomemory.mcp_server']);
  assert.equal(config.serverName, 'nanomemory');
  assert.equal(config.env.PYTHONPATH, 'C:/repo');
  assert.equal(config.env.NANOMEMORY_STORAGE_ROOT, 'C:/repo/storage');
  assert.equal(config.toolCallTimeoutMs, DEFAULT_TOOL_TIMEOUT_MS);
  assert.equal(config.failOnStartupError, false);
});

test('prepends projectRoot to an existing PYTHONPATH', () => {
  const config = bridgeConfig({ projectRoot: 'C:/repo' }, { PYTHONPATH: 'C:/other' });
  assert.equal(config.env.PYTHONPATH, `C:/repo${process.platform === 'win32' ? ';' : ':'}C:/other`);
});

test('falls back to environment variables', () => {
  const config = bridgeConfig({}, {
    NANOMEMORY_PYTHON: '/usr/bin/python3',
    NANOMEMORY_ROOT: '/srv/nanomemory',
  });
  assert.equal(config.command, '/usr/bin/python3');
  assert.equal(config.env.PYTHONPATH, '/srv/nanomemory');
});

test('rejects an invalid server name', () => {
  assert.throws(() => bridgeConfig({ serverName: 'bad name' }), /serverName/);
});

test('passes through provider and timeout overrides', () => {
  const config = bridgeConfig({ provider: 'openai_compatible', toolCallTimeoutMs: 5000, failOnStartupError: true });
  assert.equal(config.env.NANOMEMORY_PROVIDER, 'openai_compatible');
  assert.equal(config.toolCallTimeoutMs, 5000);
  assert.equal(config.failOnStartupError, true);
});
