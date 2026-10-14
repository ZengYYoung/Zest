import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { ApiError, readApi, validateConnection } from '../src/api.ts'

const originalFetch = globalThis.fetch
const token = 'test_web_' + 'x'.repeat(40)
afterEach(() => { globalThis.fetch = originalFetch })

test('model keys and external endpoint paths are rejected', async () => {
  assert.throws(() => validateConnection('sk-' + 'x'.repeat(40), 8000), ApiError)
  assert.throws(() => validateConnection(token, 0), ApiError)
  await assert.rejects(readApi({ token, port: 8000 }, 'https://external.example/api/projects'), ApiError)
})

test('requests use loopback, GET and header token without cookies or redirects', async () => {
  const seen = []
  globalThis.fetch = async (url, options) => { seen.push({ url, options }); return new Response(JSON.stringify({ data: [] }), { status: 200 }) }
  assert.deepEqual(await readApi({ token, port: 8000 }, '/api/projects'), [])
  assert.equal(seen[0].url, 'http://127.0.0.1:8000/api/projects')
  assert.equal(seen[0].options.method, 'GET')
  assert.equal(seen[0].options.headers.Authorization, `Bearer ${token}`)
  assert.equal(seen[0].options.credentials, 'omit')
  assert.equal(seen[0].options.redirect, 'error')
  assert.ok(!seen[0].url.includes(token))
})

test('unauthorized response returns stable user error without raw body', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: token }), { status: 401 })
  await assert.rejects(readApi({ token, port: 8000 }, '/api/projects'), error => error.status === 401 && !error.message.includes(token))
})

test('abort remains cancellation rather than a stale UI error', async () => {
  globalThis.fetch = async () => { throw new DOMException('cancelled', 'AbortError') }
  await assert.rejects(readApi({ token, port: 8000 }, '/api/projects'), error => error.name === 'AbortError')
})
