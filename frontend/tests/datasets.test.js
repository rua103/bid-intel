import assert from 'node:assert/strict'
import test from 'node:test'
import { createDatasetClient } from '../src/utils/datasets.js'

test('dataset header preserves JSON headers and pins the request at dispatch', async () => {
  let dataset = 'first'
  let captured
  const request = createDatasetClient(() => dataset, () => 0, async (url, options) => {
    captured = options
    dataset = 'second'
    return { ok: true, json: async () => ({ count: 1 }) }
  })
  const response = await request('/items', { headers: { 'Content-Type': 'application/json' } })
  assert.equal(captured.headers.get('X-Dataset-ID'), 'first')
  assert.equal(captured.headers.get('Content-Type'), 'application/json')
  assert.deepEqual(await response.json(), { count: 1 })
})

test('late responses and network errors are discarded after dataset switch', async () => {
  let version = 0
  const request = createDatasetClient(() => 'same-id', () => version, async () => ({
    ok: true, json: async () => { version += 2; return { old: true } },
  }))
  const response = await request('/items')
  await assert.rejects(response.json(), { name: 'AbortError' })
  const failing = createDatasetClient(() => 'same-id', () => version, async () => {
    version += 1
    throw new Error('old network error')
  })
  await assert.rejects(failing('/items'), { name: 'AbortError' })
})
