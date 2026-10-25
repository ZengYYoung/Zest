import assert from 'node:assert/strict'
import test from 'node:test'
import { dismissNotice, noticeIdentity, wasDismissed } from '../src/noticeHistory.ts'

test('dismissal survives reload, stays project-local, and does not suppress another run', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const data = new Map()
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v) } })
  try {
    const finished = { active_run_id: 'run-a', target_commit: 'commit-a', status: 'partial' }
    const identity = noticeIdentity('maintenance', finished)
    dismissNotice(8000, 'project-a', 'maintenance', identity)
    assert.equal(wasDismissed(8000, 'project-a', 'maintenance', noticeIdentity('maintenance', { ...finished })), true)
    assert.equal(wasDismissed(8000, 'project-b', 'maintenance', identity), false)
    assert.equal(wasDismissed(8001, 'project-a', 'maintenance', identity), false)
    assert.equal(wasDismissed(8000, 'project-a', 'maintenance', noticeIdentity('maintenance', { ...finished, active_run_id: 'run-b' })), false)
    assert.equal(wasDismissed(8000, 'project-a', 'maintenance', noticeIdentity('maintenance', { ...finished, status: 'pending_execution' })), false)
  } finally { if (previous) Object.defineProperty(globalThis, 'localStorage', previous); else delete globalThis.localStorage }
})

test('unavailable browser preferences cannot block normal project use', () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('disabled') } })
  try {
    assert.doesNotThrow(() => dismissNotice(8000, 'project', 'initialization', 'identity'))
    assert.equal(wasDismissed(8000, 'project', 'initialization', 'identity'), false)
  } finally { if (previous) Object.defineProperty(globalThis, 'localStorage', previous); else delete globalThis.localStorage }
})
