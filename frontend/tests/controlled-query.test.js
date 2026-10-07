import assert from 'node:assert/strict'
import test from 'node:test'
import { CONTROLLED_SCENES, buildControlledQueryRequest, controlledQueryHeaders, isControlledScene } from '../src/utils/controlledQuery.js'

test('controlled query exposes exactly five approved scenes', () => {
  assert.deepEqual(CONTROLLED_SCENES, ['awardees', 'bidders', 'co_bidders', 'common_buyers', 'common_projects'])
  assert.ok(CONTROLLED_SCENES.every(isControlledScene))
  assert.equal(isControlledScene('sql'), false)
})

test('request builder strips executable or unknown fields', () => {
  const request = buildControlledQueryRequest({ scene: 'awardees', buyer_id: 7, sql: 'DROP TABLE notices', filters: { cypher: 'MATCH (n) RETURN n' }, min_amount: 100 })
  assert.deepEqual(request, { scene: 'buyer_awardees', filters: { buyer_id: 7, min_amount: 100 }, needs_clarification: false, clarification_options: [] })
  assert.throws(() => buildControlledQueryRequest({ scene: 'arbitrary_sql' }), /白名单/)
})

test('single-subject manual queries omit the unused supplier list', () => {
  for (const scene of ['awardees', 'bidders', 'co_bidders']) {
    const request = buildControlledQueryRequest({ scene, buyer_id: 1, supplier_id: 2, supplier_ids: [] })
    assert.equal('supplier_ids' in request.filters, false)
    assert.deepEqual(Object.keys(request.filters), [scene === 'co_bidders' ? 'supplier_id' : 'buyer_id'])
  }
  assert.deepEqual(buildControlledQueryRequest({ scene: 'common_buyers', supplier_ids: ['2', '3'] }).filters, { supplier_ids: [2, 3] })
})

test('backend intent names and clarification requirements survive normalization', () => {
  const request = buildControlledQueryRequest({ scene: 'buyer_awardees', filters: { buyer_id: 7 }, needs_clarification: true, clarification_options: ['请确认采购单位'] })
  assert.equal(request.needs_clarification, true)
  assert.deepEqual(request.clarification_options, ['请确认采购单位'])
})

test('dataset isolation header is always present', () => {
  assert.equal(controlledQueryHeaders('dataset-7')['X-Dataset-ID'], 'dataset-7')
  assert.equal(controlledQueryHeaders('')['X-Dataset-ID'], 'default')
})
