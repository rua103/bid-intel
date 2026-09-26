import assert from 'node:assert/strict'
import test from 'node:test'
import { annotationId, resolveApiBase } from '../src/utils/browser.js'

test('annotation IDs work on HTTP without randomUUID, including repeated timestamps', () => {
  const ids = new Set()
  for (const prefix of ['item', 'entity', 'package', 'buyer']) {
    for (let i = 0; i < 100; i += 1) {
      const id = annotationId(prefix, {})
      assert.ok(id.startsWith(`${prefix}-`))
      assert.ok(!ids.has(id))
      ids.add(id)
    }
  }
  assert.ok(annotationId('buyer', null).startsWith('buyer-'))
  assert.equal(annotationId('item', { randomUUID: () => 'secure-id' }), 'item-secure-id')
})

test('API defaults to the page host and respects explicit deployment addresses', () => {
  assert.equal(resolveApiBase('', { protocol: 'http:', hostname: '192.168.1.20' }),
    'http://192.168.1.20:8000')
  assert.equal(resolveApiBase(undefined, { protocol: 'http:', hostname: 'localhost' }),
    'http://localhost:8000')
  assert.equal(resolveApiBase(' https://api.example.test/ ', {}), 'https://api.example.test')
  assert.equal(resolveApiBase('', { protocol: 'http:', hostname: '[::1]' }), 'http://[::1]:8000')
  assert.equal(resolveApiBase('', { protocol: 'http:', hostname: 'localhost' }, '8001'), 'http://localhost:8001')
})
