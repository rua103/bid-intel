import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, h, nextTick, reactive, ref } from 'vue'

// Mount the actual component setup code without requiring browser layout APIs.
async function loadComponent(name) {
  const sourceUrl = new URL(`../src/components/${name}.vue`, import.meta.url)
  const { descriptor } = parse(await readFile(sourceUrl, 'utf8'))
  const script = compileScript(descriptor, { id: name }).content.replace(
    /from (['"])([^'"]+)\1/g,
    (_, quote, specifier) => `from ${quote}${specifier.startsWith('.') ? new URL(specifier, sourceUrl).href : import.meta.resolve(specifier)}${quote}`,
  )
  const component = (await import(`data:text/javascript;base64,${Buffer.from(script).toString('base64')}`)).default
  return { ...component, render: () => null }
}

const renderer = createRenderer({
  createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  setText() {}, setElementText() {}, insert() {}, remove() {}, patchProp() {},
  parentNode: () => null, nextSibling: () => null,
})

async function mount(name, initialProps) {
  const component = await loadComponent(name)
  const props = reactive(initialProps)
  const instance = ref(null)
  const app = renderer.createApp({ render: () => h(component, { ...props, ref: instance }) })
  app.mount({})
  return { props, state: instance.value.$.setupState, unmount: () => app.unmount() }
}

const response = (payload) => ({ ok: true, status: 200, json: async () => payload })
const pending = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

test('manual query confirmation sends valid single-subject requests from the component', async () => {
  const requests = []
  const view = await mount('ControlledQuery', { apiBase: 'http://api.test', datasetId: 'a', fetchImpl: async (_, options) => {
    requests.push(JSON.parse(options.body).request)
    return response({ status: 'ok', source_notices: [{ id: 9, project_name: '项目九' }], payload: {} })
  } })
  try {
    for (const scene of ['awardees', 'bidders', 'co_bidders']) {
      Object.assign(view.state.manual, { scene, buyerId: '1', supplierId: '2' })
      view.state.runManual()
      await view.state.executeProposal()
    }
    assert.deepEqual(requests.map((request) => request.scene), ['buyer_awardees', 'buyer_bidders', 'supplier_co_bidders'])
    assert.ok(requests.every((request) => !('supplier_ids' in request.filters)))
    assert.ok(requests.every((request) => request.filters.include_winners === true))
    assert.equal(view.state.sourceLabel, '项目九')
  } finally { view.unmount() }
})

test('clarification responses cannot become executable proposals', async () => {
  const view = await mount('ControlledQuery', { datasetId: 'a', fetchImpl: async () => response({ status: 'clarification_required', intent: { scene: 'buyer_awardees', needs_clarification: true, clarification_options: ['采购单位甲', '采购单位乙'] } }) })
  try {
    view.state.proposal = { scene: 'awardees', buyer_id: 1 }
    view.state.question = '采购单位的供应商'
    await view.state.parseQuestion()
    assert.equal(view.state.proposal, null)
    assert.deepEqual([...view.state.ambiguities], ['采购单位甲', '采购单位乙'])
  } finally { view.unmount() }
})

test('unsupported filters show an actionable error instead of successful results', async () => {
  const view = await mount('ControlledQuery', { datasetId: 'a', fetchImpl: async () => response({ status: 'unsupported_filter', detail: '请清除时间过滤条件后再执行' }) })
  try {
    view.state.proposal = { scene: 'awardees', buyer_id: 1, start_date: '2025-01-01' }
    await view.state.executeProposal()
    assert.equal(view.state.result, null)
    assert.match(view.state.error, /清除时间/)
  } finally { view.unmount() }
})

test('dataset switches clear previous state and discard delayed query responses', async () => {
  const delayed = pending()
  const view = await mount('ControlledQuery', { datasetId: 'a', fetchImpl: () => delayed.promise })
  try {
    view.state.proposal = { scene: 'awardees', buyer_id: 1 }
    view.state.result = { payload: 'old dataset' }
    view.state.manual.buyerId = '1'
    const executing = view.state.executeProposal()
    view.props.datasetId = 'b'
    await nextTick()
    assert.equal(view.state.proposal, null)
    assert.equal(view.state.result, null)
    assert.equal(view.state.manual.buyerId, '')
    assert.equal(view.state.manual.includeAwardee, true)
    delayed.resolve(response({ status: 'ok', payload: 'dataset a response' }))
    await executing
    assert.equal(view.state.result, null)
    assert.equal(view.state.error, '')
  } finally { view.unmount() }
})

test('relation clues include the reviewer session and current dataset header', async (t) => {
  let sent
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    sent = { url, options }
    return response({ clues: [] })
  })
  const view = await mount('RelationshipClues', { apiBase: 'http://api.test', datasetId: 'a' })
  try {
    assert.equal(sent.options.credentials, 'include')
    assert.equal(sent.options.headers.get('X-Dataset-ID'), 'a')
    assert.equal(new URL(sent.url).searchParams.has('include_winners'), false, 'inherit the API default instead of overriding it to exclude winners')
  } finally { view.unmount() }
})

test('relation API entities, numeric metrics, definitions and time basis have readable labels', async () => {
  const view = await mount('RelationshipClues', { apiBase: 'http://api.test', fetchImpl: async () => response({ clues: [] }) })
  try {
    const scope = {
      participation_rule: '统计中标、未中标和结果未披露的参与记录',
      participation_outcomes: ['winner', 'nonwinner', 'unknown'],
      include_winners: true,
      award_rule: '中标项目/包仅按 awards 记录统计，保留多个中标方',
      project_deduplication: '按 project_id 去重',
      package_deduplication: '按 package_id 保留并展示',
      default_package_rule: '保留 package_code=default，并逐条标示包号待核查',
      time_basis: '公告导入时间：notices.created_at（当前数据模型未保存公告发布时间）',
    }
    const clue = {
      entities: [{ organization_id: 1, canonical_name: '供应商甲' }, { organization_id: 2, canonical_name: '供应商乙' }],
      metrics: { project_count: 2, package_count: 3, relationship_rule: '同一采购包存在投标记录' },
      time_range: { start: '2026-10-01', end: '2026-10-07', basis: scope.time_basis },
      scope,
    }
    assert.deepEqual(view.state.participantsOf(clue).map(view.state.participantLabel), ['供应商甲', '供应商乙'])
    assert.deepEqual(view.state.metricsOf(clue), { 项目数: 2, 采购包数: 3 })
    assert.match(view.state.timeRangeOf(clue), /2026-10-01 至 2026-10-07（时间依据：公告导入时间/)
    assert.match(view.state.timeRangeOf(clue), /未保存公告发布时间/)
    const [entry] = view.state.semanticsEntries({ common_bidding: { label: '供应商共同投标', description: '同一采购包中的主体关系', scope } })
    assert.equal(view.state.semanticLabel(entry), '供应商共同投标')
    const explanation = view.state.semanticText(entry)
    assert.match(explanation, /同一采购包中的主体关系/)
    assert.match(explanation, /参与口径：统计中标、未中标和结果未披露/)
    assert.match(explanation, /参与结果：中标、未中标、结果未披露/)
    assert.match(explanation, /包含中标方：是/)
    assert.match(explanation, /默认包号：保留 package_code=default/)
    assert.match(explanation, /时间依据：公告导入时间/)
    assert.doesNotMatch(explanation, /\[object Object\]|"scope"/)

    assert.deepEqual(view.state.participantsOf({ participants: ['旧格式主体'] }), ['旧格式主体'])
    assert.deepEqual(view.state.semanticsEntries({ 统计口径: '旧格式说明' }), [{ label: '统计口径', text: '旧格式说明' }])
    assert.equal(view.state.semanticText({ text: '旧格式说明' }), '旧格式说明')
    assert.equal(view.state.timeRangeOf({ time_range: { from: '起始', to: '结束' } }), '起始 至 结束')
  } finally { view.unmount() }
})

test('controlled query labels unknown outcomes and exposes participation scope', async () => {
  const view = await mount('ControlledQuery', { apiBase: 'http://api.test', datasetId: 'a', fetchImpl: async () => response({ status: 'ok', payload: {} }) })
  try {
    assert.equal(view.state.outcomeLabel('winner'), '中标')
    assert.equal(view.state.outcomeLabel('nonwinner'), '未中标')
    assert.equal(view.state.outcomeLabel('unknown'), '结果未披露')
    assert.match(view.state.participationLabel({ include_winners: true }), /包含中标/)
    assert.equal(view.state.participationLabel({ include_winners: false }), '仅统计未中标；不包含中标和结果未披露')
  } finally { view.unmount() }
})

test('controlled query uses nested API filters for parsed and executed participation scope', async () => {
  const view = await mount('ControlledQuery', {
    apiBase: 'http://api.test', datasetId: 'a',
    fetchImpl: async (url) => response(url.endsWith('/parse')
      ? { status: 'ready', intent: { scene: 'buyer_bidders', filters: { buyer_id: 1, include_winners: true } } }
      : { status: 'ok', scene: 'buyer_bidders', filters: { buyer_id: 1, include_winners: true }, payload: {} }),
  })
  try {
    await view.state.parseQuestion()
    assert.equal(view.state.participationLabel(view.state.proposal), '包含中标、未中标和结果未披露')
    await view.state.executeProposal()
    assert.equal(view.state.participationLabel(view.state.result), '包含中标、未中标和结果未披露')
    Object.assign(view.state.manual, { scene: 'bidders', buyerId: '1', includeAwardee: false })
    view.state.runManual()
    assert.equal(view.state.participationLabel(view.state.proposal), '仅统计未中标；不包含中标和结果未披露')
    assert.equal(view.state.supportsParticipationFilter('common_projects'), false)
    assert.equal(view.state.participationLabel({ scene: 'common_projects', filters: { include_winners: false } }), '包含中标、未中标和结果未披露')
  } finally { view.unmount() }
})
