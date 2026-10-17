// `claude plugin test plugin`: the usage bars' figures reach the file the sidebar reads.
import { expect, mock, test } from 'claude-code/testing'

const CONTEXT = { tokens: 1000, window: 200000, percent: 1 }
const LIMITS = [
  { kind: 'five_hour', percentUsed: 23.5, resetsAt: '2026-10-17T18:00:00Z' },
  { kind: 'seven_day', percentUsed: 41, resetsAt: '2026-10-21T09:00:00Z' },
]

// the engine beneath the plugin: it answers the measurement, and keeps what is written
function written(on) {
  const files: { path: string; text: string }[] = []
  on('session.measure', async ($, e) => ({ changed: e.changed }))
  on('fs.write', async ($, e) => {
    files.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  return files
}

test('a measured window is written to the runtime folder', async ($, on) => {
  mock.env(on, { XDG_RUNTIME_DIR: '/run/user/1000' })
  mock.clock(on, { now: 1792250000000 })
  const files = written(on)
  await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['rateLimits'] })
  expect(files.map(f => f.path)).toEqual(['/run/user/1000/sidelit/usage.json'])
  expect(JSON.parse(files[0].text)).toEqual({ at: 1792250000000, limits: LIMITS })
})

test('a test instance writes to its own folder', async ($, on) => {
  mock.env(on, { XDG_RUNTIME_DIR: '/run/user/1000', SIDELIT_HOME: '/tmp/sidelit-test' })
  mock.clock(on)
  const files = written(on)
  await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['rateLimits', 'context'] })
  expect(files.map(f => f.path)).toEqual(['/tmp/sidelit-test/usage.json'])
})

test('nothing is written when the limits did not move, or there are none', async ($, on) => {
  mock.env(on, { XDG_RUNTIME_DIR: '/run/user/1000' })
  mock.clock(on)
  const files = written(on)
  await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['context'] })
  await $.session.measure({ context: CONTEXT, rateLimits: [], changed: ['rateLimits'] })  // (off a subscription)
  expect(files).toEqual([])
})

test('a write that fails is not Claude\'s problem', async ($, on) => {
  mock.env(on, { XDG_RUNTIME_DIR: '/nonexistent' })
  mock.clock(on)
  on('session.measure', async ($, e) => ({ changed: e.changed }))
  on('fs.write', async () => { throw new Error('EACCES') })
  const result = await $.session.measure({ context: CONTEXT, rateLimits: LIMITS, changed: ['rateLimits'] })
  expect(result.changed).toEqual(['rateLimits'])
})
