import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

const nowMs = 1_700_000_000_000;

function encodeSegment(value) {
  return Buffer.from(JSON.stringify(value), 'utf8')
    .toString('base64url');
}

function tokenWithPayload(payload) {
  return `${encodeSegment({ alg: 'none' })}.${encodeSegment(payload)}.signature`;
}

async function loadAuthTokenModule() {
  const source = await readFile(new URL('../src/authToken.ts', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
      strict: true,
    },
  });
  const dir = await mkdtemp(join(tmpdir(), 'auth-token-test-'));
  const modulePath = join(dir, 'authToken.mjs');
  await writeFile(modulePath, compiled.outputText, 'utf8');

  try {
    return await import(`file://${modulePath}`);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

const { parseDashboardToken } = await loadAuthTokenModule();

const validToken = tokenWithPayload({ email: 'user@example.com', exp: nowMs / 1000 + 120 });
assert.deepEqual(parseDashboardToken(validToken, nowMs), {
  email: 'user@example.com',
  expiresAt: nowMs + 120_000,
});

for (const [name, token] of [
  ['malformed token', 'not-a-jwt'],
  ['invalid json payload', 'header.not-json.signature'],
  ['array payload', tokenWithPayload(['user@example.com'])],
  ['missing email', tokenWithPayload({ exp: nowMs / 1000 + 120 })],
  ['non-string email', tokenWithPayload({ email: 123, exp: nowMs / 1000 + 120 })],
  ['empty email', tokenWithPayload({ email: '   ', exp: nowMs / 1000 + 120 })],
  ['missing exp', tokenWithPayload({ email: 'user@example.com' })],
  ['non-numeric exp', tokenWithPayload({ email: 'user@example.com', exp: 'soon' })],
  ['infinite exp', tokenWithPayload({ email: 'user@example.com', exp: Infinity })],
  ['expired exp', tokenWithPayload({ email: 'user@example.com', exp: nowMs / 1000 })],
]) {
  assert.equal(parseDashboardToken(token, nowMs), null, name);
}

console.log('auth token tests passed');
