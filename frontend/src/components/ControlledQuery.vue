<template>
  <section class="controlled-query" aria-labelledby="controlled-query-title">
    <header class="controlled-query__header">
      <div>
        <p class="eyebrow">受控自然语言查询</p>
        <h2 id="controlled-query-title">用问题访问已批准的分析</h2>
        <p class="muted">只会映射到五类只读查询。执行前请确认识别出的场景和过滤条件。</p>
      </div>
      <span class="readonly-badge">只读</span>
    </header>

    <form class="controlled-query__form" @submit.prevent="parseQuestion">
      <label for="controlled-query-input">问题</label>
      <textarea id="controlled-query-input" v-model="question" rows="3" maxlength="1000"
        placeholder="例如：查询 2025 年采购单位 A 中标的供应商"></textarea>
      <div class="controlled-query__actions">
        <button class="primary" type="submit" :disabled="busy || !question.trim()">{{ busy ? '识别中…' : '识别查询' }}</button>
        <button class="secondary" type="button" :disabled="busy" @click="showManual = !showManual">{{ showManual ? '收起手动查询' : '手动选择场景' }}</button>
      </div>
    </form>

    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <div v-if="ambiguities.length" class="controlled-query__ambiguity" role="status">
      <strong>需要补充：</strong>
      <p>{{ ambiguityMessage }}</p>
      <div class="controlled-query__options">
        <span v-for="(option, index) in ambiguities" :key="index">{{ typeof option === 'string' ? option : option.label || option.name || option.value }}</span>
      </div>
    </div>

    <form v-if="showManual" class="controlled-query__manual" @submit.prevent="runManual">
      <h3>手动选择</h3>
      <label>查询场景
        <select v-model="manual.scene" required>
          <option disabled value="">请选择</option>
          <option v-for="scene in scenes" :key="scene.value" :value="scene.value">{{ scene.label }}</option>
        </select>
      </label>
      <label v-if="manualNeedsBuyer">采购单位
        <select v-model="manual.buyerId" required>
          <option value="">请选择采购单位</option>
          <option v-for="organization in organizations" :key="organization.id" :value="String(organization.id)">{{ organization.canonical_name }}</option>
        </select>
      </label>
      <label v-if="manualNeedsSupplier">供应商
        <select v-model="manual.supplierId" required>
          <option value="">请选择供应商</option>
          <option v-for="organization in organizations" :key="organization.id" :value="String(organization.id)">{{ organization.canonical_name }}</option>
        </select>
      </label>
      <label v-if="manualNeedsSupplierList">供应商（可多选）
        <select v-model="manual.supplierIds" multiple size="5" required>
          <option v-for="organization in organizations" :key="organization.id" :value="String(organization.id)">{{ organization.canonical_name }}</option>
        </select>
      </label>
      <p v-if="organizationsLoading" class="muted">正在读取主体目录…</p>
      <p v-if="organizationsError" class="error">{{ organizationsError }}</p>
      <div class="controlled-query__grid">
        <label>开始日期<input v-model="manual.startDate" type="date" /></label>
        <label>结束日期<input v-model="manual.endDate" type="date" /></label>
        <label>最低金额<input v-model.number="manual.minAmount" type="number" min="0" step="0.01" /></label>
        <label>最高金额<input v-model.number="manual.maxAmount" type="number" min="0" step="0.01" /></label>
      </div>
      <label class="checkbox"><input v-model="manual.includeAwardee" type="checkbox" /> 包含中标方</label>
      <button class="primary" type="submit" :disabled="busy || !manual.scene">执行手动查询</button>
    </form>

    <div v-if="proposal" class="controlled-query__proposal">
      <h3>请确认查询口径</h3>
      <dl>
        <div><dt>场景</dt><dd>{{ sceneLabel(proposal.scene) }}</dd></div>
        <div><dt>采购单位</dt><dd>{{ proposal.buyer || proposal.filters?.buyer_id || proposal.buyer_id || '未指定' }}</dd></div>
        <div><dt>供应商</dt><dd>{{ proposal.supplier || proposal.filters?.supplier_id || proposal.supplier_id || proposal.filters?.supplier_ids?.join('、') || proposal.supplier_ids?.join('、') || '未指定' }}</dd></div>
        <div><dt>时间范围</dt><dd>{{ formatRange(proposal.filters?.start_date ?? proposal.start_date, proposal.filters?.end_date ?? proposal.end_date) }}</dd></div>
        <div><dt>金额范围</dt><dd>{{ formatAmount(proposal.filters?.min_amount ?? proposal.min_amount, proposal.filters?.max_amount ?? proposal.max_amount) }}</dd></div>
        <div><dt>包含中标方</dt><dd>{{ (proposal.filters?.include_winners ?? proposal.include_awardee) ? '是' : '否' }}</dd></div>
      </dl>
      <p v-if="proposal.explanation" class="muted">{{ proposal.explanation }}</p>
      <div class="controlled-query__actions">
        <button class="primary" type="button" :disabled="busy" @click="executeProposal">确认并执行</button>
        <button class="secondary" type="button" :disabled="busy" @click="proposal = null">修改</button>
      </div>
    </div>

    <div v-if="result" class="controlled-query__result">
      <h3>查询结果</h3>
      <dl class="result-meta">
        <div><dt>查询口径</dt><dd>{{ result.semantics || result.query_semantics || '受控自然语言映射' }}</dd></div>
        <div><dt>分析后端</dt><dd>{{ result.backend || result.analytics_backend || '未返回' }}</dd></div>
        <div><dt>来源公告</dt><dd>{{ sourceLabel }}</dd></div>
      </dl>
      <pre class="result-json">{{ JSON.stringify(result.payload ?? result.data ?? result, null, 2) }}</pre>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { formatAmount as formatMoney } from '../utils/amountPolicy.js'
import { buildControlledQueryRequest, controlledQueryHeaders } from '../utils/controlledQuery.js'
import { createDatasetClient } from '../utils/datasets.js'

const props = defineProps({
  apiBase: { type: String, default: '' },
  datasetId: { type: String, default: 'default' },
  fetchImpl: { type: Function, default: (...args) => fetch(...args) },
})
const emit = defineEmits(['executed', 'error'])
let datasetVersion = 0
const datasetFetch = createDatasetClient(() => props.datasetId, () => datasetVersion, (...args) => props.fetchImpl(...args))

const scenes = [
  { value: 'awardees', label: '采购单位的中标供应商' },
  { value: 'bidders', label: '采购单位的投标主体与组合' },
  { value: 'co_bidders', label: '供应商的共同竞标方' },
  { value: 'common_buyers', label: '供应商共同合作采购单位' },
  { value: 'common_projects', label: '供应商共同投标项目' },
]
const question = ref('')
const proposal = ref(null)
const result = ref(null)
const error = ref('')
const ambiguities = ref([])
const ambiguityMessage = ref('')
const busy = ref(false)
const showManual = ref(false)
const organizations = ref([])
const organizationsLoading = ref(false)
const organizationsError = ref('')
const emptyManual = () => ({ scene: '', buyerId: '', supplierId: '', supplierIds: [], startDate: '', endDate: '', minAmount: null, maxAmount: null, includeAwardee: false })
const manual = reactive(emptyManual())

const sceneLabel = (value) => scenes.find((item) => item.value === value || ({ awardees: 'buyer_awardees', bidders: 'buyer_bidders', co_bidders: 'supplier_co_bidders' })[item.value] === value)?.label || value || '未指定'
const formatRange = (start, end) => start || end ? `${start || '不限'} 至 ${end || '不限'}` : '不限'
const formatAmount = (min, max) => min != null || max != null ? `¥${formatMoney(min ?? 0)} 至 ¥${max == null ? '不限' : formatMoney(max)}` : '不限'
const manualNeedsBuyer = computed(() => ['awardees', 'bidders'].includes(manual.scene))
const manualNeedsSupplier = computed(() => manual.scene === 'co_bidders')
const manualNeedsSupplierList = computed(() => ['common_buyers', 'common_projects'].includes(manual.scene))
const sourceLabel = computed(() => {
  const sources = result.value?.sources || result.value?.source_notices || result.value?.data?.sources || result.value?.data?.source_notices
  if (!Array.isArray(sources) || !sources.length) return '后端未返回公告引用'
  return sources.map((item) => typeof item === 'string' ? item : item.title || item.project_name || item.notice_id || item.id || item.source_file).filter(Boolean).join('、')
})
function endpoint(path) { return `${String(props.apiBase || '').replace(/\/$/, '')}/api/v1/controlled-query${path}` }
async function request(path, body) {
  const response = await datasetFetch(endpoint(path), { method: 'POST', headers: controlledQueryHeaders(props.datasetId), body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail || payload.message || `请求失败（${response.status}）`)
  return payload
}

async function parseQuestion() {
  const current = datasetVersion
  busy.value = true; error.value = ''; ambiguities.value = []; result.value = null; proposal.value = null
  try {
    const payload = await request('/parse', { question: question.value })
    if (payload.status === 'clarification_required' || payload.intent?.needs_clarification || payload.status === 'ambiguous' || payload.ambiguities?.length) {
      ambiguities.value = payload.intent?.clarification_options?.length ? payload.intent.clarification_options : payload.ambiguities?.length ? payload.ambiguities : ['请明确场景和主体。']
      ambiguityMessage.value = payload.message || '请补充问题后重新识别，或使用手动查询选择主体。'
    } else if (payload.status === 'rejected') {
      throw new Error(payload.reason || payload.message || '问题不在允许的查询范围内。')
    } else {
      proposal.value = payload.request || payload.intent || payload
    }
  } catch (cause) {
    if (cause.name !== 'AbortError') { error.value = cause.message; emit('error', cause) }
  } finally { if (current === datasetVersion) busy.value = false }
}

function runManual() {
  error.value = ''; ambiguities.value = []; result.value = null
  proposal.value = {
    scene: manual.scene,
    buyer_id: manual.buyerId ? Number(manual.buyerId) : null,
    supplier_id: manual.supplierId ? Number(manual.supplierId) : null,
    supplier_ids: manual.supplierIds.map(Number),
    start_date: manual.startDate || null,
    end_date: manual.endDate || null,
    min_amount: manual.minAmount ?? null,
    max_amount: manual.maxAmount ?? null,
    include_awardee: manual.includeAwardee,
  }
}

async function loadOrganizations() {
  if (organizations.value.length || organizationsLoading.value) return
  const current = datasetVersion
  organizationsLoading.value = true
  organizationsError.value = ''
  try {
    const response = await datasetFetch(
      `${String(props.apiBase || '').replace(/\/$/, '')}/api/v1/organizations?limit=500`,
      { headers: { 'X-Dataset-ID': props.datasetId } },
    )
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '主体目录读取失败')
    organizations.value = Array.isArray(payload) ? payload : payload.organizations || []
  } catch (cause) {
    if (cause.name !== 'AbortError') organizationsError.value = cause.message || '主体目录读取失败'
  } finally {
    if (current === datasetVersion) organizationsLoading.value = false
  }
}

async function executeProposal() {
  if (!proposal.value) return
  const current = datasetVersion
  busy.value = true; error.value = ''; result.value = null
  try {
    const payload = await request('/execute', { request: buildControlledQueryRequest(proposal.value) })
    if (payload.status === 'unsupported_filter') throw new Error(payload.detail || '当前查询不支持所选过滤条件。')
    if (payload.status === 'clarification_required') {
      ambiguities.value = payload.options?.length ? payload.options : ['请明确场景和主体。']
      ambiguityMessage.value = '请补充问题后重新识别，或使用手动查询选择主体。'
      proposal.value = null
      return
    }
    result.value = payload
    emit('executed', payload)
  } catch (cause) {
    if (cause.name !== 'AbortError') { error.value = cause.message; emit('error', cause) }
  } finally { if (current === datasetVersion) busy.value = false }
}

watch(showManual, (visible) => { if (visible) loadOrganizations() })
watch(() => props.datasetId, () => {
  datasetVersion += 1
  proposal.value = null
  result.value = null
  error.value = ''
  ambiguities.value = []
  ambiguityMessage.value = ''
  busy.value = false
  organizations.value = []
  organizationsLoading.value = false
  organizationsError.value = ''
  Object.assign(manual, emptyManual())
  if (showManual.value) loadOrganizations()
}, { flush: 'sync' })
onBeforeUnmount(() => { datasetVersion += 1 })

defineExpose({ parseQuestion, executeProposal, runManual })
</script>

<style scoped>
.controlled-query { display: grid; gap: 1rem; padding: 1.25rem; border: 1px solid var(--line, #d8dee8); background: var(--panel, #fff); }
.controlled-query__header, .controlled-query__actions, .controlled-query__options { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
.controlled-query__header { justify-content: space-between; }
.controlled-query h2, .controlled-query h3 { margin: 0; }
.controlled-query label { display: grid; gap: .35rem; font-weight: 600; }
.controlled-query textarea, .controlled-query input, .controlled-query select { width: 100%; border: 1px solid var(--line, #cbd5e1); padding: .6rem; font: inherit; box-sizing: border-box; }
.controlled-query__form, .controlled-query__manual, .controlled-query__proposal, .controlled-query__result { display: grid; gap: .75rem; }
.controlled-query__manual, .controlled-query__proposal, .controlled-query__result, .controlled-query__ambiguity { padding: 1rem; background: var(--surface, #f8fafc); border: 1px solid var(--line, #d8dee8); }
.controlled-query__grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .75rem; }
.controlled-query dl { display: grid; gap: .45rem; margin: 0; }
.controlled-query dl div { display: grid; grid-template-columns: 7rem 1fr; gap: .5rem; }
.controlled-query dt { color: var(--muted, #64748b); }
.controlled-query dd { margin: 0; }
.readonly-badge { padding: .25rem .5rem; border: 1px solid #94a3b8; color: #475569; font-size: .8rem; }
.muted { color: var(--muted, #64748b); margin: 0; }
.error { color: #b91c1c; }
.result-json { overflow: auto; max-height: 20rem; background: #0f172a; color: #e2e8f0; padding: .75rem; font-size: .8rem; }
.checkbox { display: flex !important; grid-template-columns: auto 1fr; align-items: center; }
.checkbox input { width: auto; }
</style>
