import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { ApiError, prepareCommand, readApi, writeApi, validateConnection } from '../src/api.ts'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

test('connection has only a validated local port', async () => {
  assert.deepEqual(validateConnection(8000), { port: 8000 })
  for (const port of [0, 65536, 1.5, NaN]) assert.throws(() => validateConnection(port), ApiError)
  await assert.rejects(readApi({ port: 8000 }, 'https://external.example/api/projects'), ApiError)
})

test('GET uses loopback without tokens, cookies or redirects', async () => {
  const seen = []
  globalThis.fetch = async (url, options) => { seen.push({ url, options }); return new Response(JSON.stringify({ data: [] }), { status: 200 }) }
  assert.deepEqual(await readApi({ port: 8000 }, '/api/projects'), [])
  assert.equal(seen[0].url, 'http://127.0.0.1:8000/api/projects')
  assert.equal(seen[0].options.method, 'GET')
  assert.equal(seen[0].options.headers, undefined)
  assert.equal(seen[0].options.credentials, 'omit')
  assert.equal(seen[0].options.redirect, 'error')
})

test('POST carries a stable request identity and never retries by itself', async () => {
  const command = prepareCommand('task.create', { project_id: 'fixture', question: 'Why?' })
  const seen = []
  globalThis.fetch = async (url, options) => { seen.push(options); throw new TypeError('disconnected') }
  await assert.rejects(writeApi({ port: 8000 }, '/api/operations', command), /回执/)
  assert.equal(seen.length, 1)
  await assert.rejects(writeApi({ port: 8000 }, '/api/operations', command))
  assert.equal(JSON.parse(seen[0].body).request_id, JSON.parse(seen[1].body).request_id)
  assert.equal(seen[0].headers['Content-Type'], 'application/json')
  assert.equal(seen[0].method, 'POST')
})

test('validation errors do not expose raw response inputs', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'private input' }), { status: 422 })
  await assert.rejects(readApi({ port: 8000 }, '/api/projects'), error => error.status === 422 && !error.message.includes('private input'))
})

test('abort remains cancellation rather than a stale UI error', async () => {
  globalThis.fetch = async () => { throw new DOMException('cancelled', 'AbortError') }
  await assert.rejects(readApi({ port: 8000 }, '/api/projects'), error => error.name === 'AbortError')
})

test('lost key-save responses lead to configuration readback instead of operation replay', async () => {
  let calls = 0
  globalThis.fetch = async () => { calls++; throw new TypeError('disconnected') }
  await assert.rejects(writeApi({ port: 8000 }, '/api/model/settings', { api_key: 'offline-fixture-key' }), /重新打开设置核对/)
  assert.equal(calls, 1)
})
