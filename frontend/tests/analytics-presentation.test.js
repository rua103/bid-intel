import assert from 'node:assert/strict'
import test from 'node:test'
import { participationScopeLabel, productSupplierEvidence, productSupplierMetrics } from '../src/utils/analyticsPresentation.js'

test('brand aggregates show only disclosed item totals and do not turn missing values into zero', () => {
  assert.equal(productSupplierMetrics({ project_count: 2, package_count: 3, amount_total: '1020.255' }), '2 个项目 · 3 个采购包 · 已披露标的总价合计 ¥1,020.26')
  assert.equal(productSupplierMetrics({ amount_total: null }), '未披露标的总价')
  assert.equal(productSupplierMetrics({ amount_total: '0' }), '已披露标的总价合计 ¥0.00')
  assert.equal(productSupplierMetrics({ transaction_amount_total: '1000' }), '未披露标的总价')
  assert.equal(productSupplierMetrics({ name: '品牌甲', legacy: true }), '')
})

test('empty evidence arrays or identifiers alone never claim to contain source excerpts', () => {
  for (const row of [{ evidence: [] }, { evidence: [{}] }, { evidence: [{ item_id: 1 }] }, { source_evidence: ' ', evidence_count: 3 }]) {
    assert.equal(productSupplierEvidence(row), '未保存来源证据')
  }
  assert.equal(productSupplierEvidence({ evidence: [{ source_file: '原文.html', source_location: '第 1 表', source_evidence: '' }] }), '有来源定位，未保存证据片段')
  assert.equal(productSupplierEvidence({ source_evidence: '', evidence: [{ source_evidence: '品牌甲  单价 100 元' }] }), '有记录级证据片段')
})

test('participation displays returned evidence without inventing a default for missing metadata', () => {
  assert.equal(participationScopeLabel({ payload: { include_winners: true } }), '包含中标、未中标和结果未披露')
  assert.equal(participationScopeLabel({ filters: { include_winners: false }, include_winners: true }), '仅统计未中标；不包含中标和结果未披露')
  assert.equal(participationScopeLabel({ semantics: { participation_outcomes: ['unknown'] }, include_winners: false }), '结果未披露')
  assert.equal(participationScopeLabel({ scene: 'buyer_awardees', filters: { include_winners: true } }), '按中标记录统计')
  assert.equal(participationScopeLabel({}), '未返回参与口径')
})
