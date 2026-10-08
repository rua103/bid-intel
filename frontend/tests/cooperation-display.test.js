import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { parse } from '@vue/compiler-sfc'
import { compile } from '@vue/compiler-dom'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import { formatAmount } from '../src/utils/amountPolicy.js'
import { productSupplierEvidence, productSupplierMetrics } from '../src/utils/analyticsPresentation.js'

const { descriptor } = parse(await readFile(new URL('../src/App.vue', import.meta.url), 'utf8'))

async function renderResult(scene, endMarker, payload) {
  const start = descriptor.template.content.indexOf(`<div v-if="analyticsResults.${scene}"`)
  const end = descriptor.template.content.indexOf(endMarker, start)
  const source = descriptor.template.content.slice(start, end)
  const render = new Function('Vue', compile(source, { mode: 'function', prefixIdentifiers: true }).code)(Vue)
  return renderToString(Vue.createSSRApp({
    render,
    setup: () => ({
      analyticsResults: { [scene]: payload }, prettyAmount: formatAmount,
      productSupplierRows: () => [], productSuppliersOf: () => [],
      productSupplierName: (row) => row.name, productSupplierEvidence, productSupplierMetrics,
    }),
  }))
}

test('scene one renders project frequency separately from the retained package count', async () => {
  const html = await renderResult('awardees', '</article>', {
    awardees: [{ organization_id: 1, name: 'Supplier', award_project_count: 1, award_package_count: 2, award_amount_total: '0.20' }],
  })
  assert.match(html, /1 个合作项目 · 2 个采购包 · ¥0\.20/)
})

test('scene four renders separate supplier project frequencies and the buyer intersection boundary', async () => {
  const html = await renderResult('commonBuyers', '<div v-if="analyticsResults.commonProjects"', {
    buyers: [{ buyer: { id: 1, canonical_name: 'Buyer' }, award_amount_total_unique_awards: null,
      suppliers: [{ name: 'A', award_project_count: 1, award_package_count: 2 }, { name: 'B', award_project_count: 2, award_package_count: 2 }],
    }],
  })
  assert.match(html, /A：1 个合作项目 · 2 个采购包/)
  assert.match(html, /B：2 个合作项目 · 2 个采购包/)
  assert.match(html, /各供应商分别统计/)
  assert.match(html, /不要求在同一项目或同一采购包中标/)
  assert.doesNotMatch(html, /¥0\.00/)
})
