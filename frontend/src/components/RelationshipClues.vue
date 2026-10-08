<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createDatasetClient } from '../utils/datasets.js'

const props = defineProps({
  apiBase: { type: String, required: true },
  datasetId: { type: String, default: 'default' },
  refreshKey: { type: [String, Number], default: 0 },
  fetchImpl: { type: Function, default: (url, options = {}) => fetch(url, { ...options, credentials: 'include' }) },
  // Keep the endpoint configurable so deployments can expose a versioned route
  // without making the panel depend on the rest of App.vue.
  endpoint: { type: String, default: '' },
})
const emit = defineEmits(['open-notice'])

const loading = ref(false)
const error = ref('')
const payload = ref(null)
let version = 0
const datasetFetch = createDatasetClient(() => props.datasetId, () => version, (...args) => props.fetchImpl(...args))

const endpointUrl = computed(() => {
  if (props.endpoint) return props.endpoint
  return `${props.apiBase}/api/v1/analytics/relation-clues?kind=all`
})
const clues = computed(() => Array.isArray(payload.value?.clues) ? payload.value.clues : [])
const definitions = computed(() => payload.value?.definitions || payload.value?.semantics || {})
const scope = computed(() => payload.value?.scope || payload.value?.summary || {})
const metricLabels = {
  project_count: '项目数',
  package_count: '采购包数',
  participation_project_count: '参与项目数',
  award_project_count: '中标项目数',
  participation_package_count: '参与采购包数',
  award_package_count: '中标采购包数',
}
const metricsOf = (clue) => Object.fromEntries(Object.entries(clue?.metrics || {})
  .filter(([, value]) => typeof value === 'number' && Number.isFinite(value))
  .map(([key, value]) => [metricLabels[key] || key, value]))
const typeLabels = {
  'common-bidding': '供应商共同投标关系',
  common_bidding: '供应商共同投标关系',
  common_bid: '供应商共同投标关系',
  'repeat-cooperation': '跨项目重复合作',
  repeat_cooperation: '跨项目重复合作',
  repeated_cooperation: '跨项目重复合作',
  'supplier-distribution': '供应商参与与中标分布',
  supplier_distribution: '供应商参与与中标分布',
  'buyer-network': '采购单位与供应商关系网络',
  buyer_network: '采购单位与供应商关系网络',
}

function typeLabel(clue) {
  const type = clue?.type || clue?.clue_type || clue?.kind || ''
  return typeLabels[type] || type || '关系线索'
}

function listValue(value) {
  if (Array.isArray(value)) return value
  if (value == null || value === '') return []
  return [value]
}

function participantsOf(clue) {
  return listValue(clue?.entities ?? clue?.participants)
}

function evidenceValue(clue, key) {
  const nested = clue?.evidence?.[key]
  if (nested != null) return nested
  return clue?.[key]
}

function noticesOf(clue) {
  return listValue(evidenceValue(clue, 'notices') ?? clue?.notice_ids)
}

function projectsOf(clue) {
  return listValue(evidenceValue(clue, 'projects') ?? clue?.project_ids)
}

function packagesOf(clue) {
  return listValue(evidenceValue(clue, 'packages') ?? clue?.package_ids)
}

function warningsOf(clue) {
  return listValue(clue?.warnings ?? clue?.evidence?.warnings)
}

function linksOf(clue) {
  const links = clue?.evidence_links ?? clue?.links ?? clue?.evidence?.links
  if (Array.isArray(links) && links.length) return links

  // Some early payloads only returned notice IDs. Keep those records actionable
  // so the panel can still open the existing evidence dialog.
  return noticesOf(clue).map((entry) => ({
    notice_id: noticeIdOf(entry),
    item_id: itemIdOf(entry),
    label: '查看公告与证据',
  })).filter((entry) => entry.notice_id != null)
}

function noticeIdOf(entry) {
  if (entry == null || typeof entry !== 'object') return entry
  return entry.notice_id ?? entry.noticeId ?? entry.id ?? null
}

function itemIdOf(entry) {
  if (entry == null || typeof entry !== 'object') return null
  return entry.item_id ?? entry.itemId ?? null
}

function projectIdOf(entry) {
  if (entry == null || typeof entry !== 'object') return entry
  return entry.project_id ?? entry.projectId ?? entry.id ?? null
}

function packageCodeOf(entry) {
  if (entry == null || typeof entry !== 'object') return entry
  return entry.package_code ?? entry.package_number ?? entry.code ?? entry.package_id ?? entry.id ?? null
}

function participantLabel(entry) {
  if (entry == null) return '—'
  if (typeof entry !== 'object') return String(entry)
  return entry.name ?? entry.canonical_name ?? entry.organization_name ?? entry.organization ?? entry.id ?? '未命名主体'
}

function projectLabel(entry) {
  if (entry == null) return '—'
  if (typeof entry !== 'object') return String(entry)
  return entry.project_name ?? entry.name ?? entry.project_number ?? entry.project_id ?? entry.id ?? '未命名项目'
}

function packageLabel(entry) {
  const code = packageCodeOf(entry)
  if (code == null || code === '') return '未记录包号'
  return String(code).toLowerCase() === 'default'
    ? 'default（系统默认，非原文包号）'    : String(code)
}

function noticeLabel(entry) {
  if (entry == null) return '未记录公告'
    if (typeof entry !== 'object') return '公告 #' + entry
  const id = noticeIdOf(entry)
  const title = entry.title ?? entry.notice_title ?? entry.project_name ?? ''
  return title || (id != null ? '公告 #' + id : '未命名公告')
}

function timeRangeOf(clue) {
  const value = clue?.time_range ?? clue?.evidence?.time_range
  let range = value == null || value === '' ? '未记录' : String(value)
  if (value && typeof value === 'object') {
    const start = value.start ?? value.from ?? value.begin ?? value.start_date
    const end = value.end ?? value.to ?? value.finish ?? value.end_date
    range = start || end ? (start || '未记录') + ' 至 ' + (end || '未记录') : value.label ?? value.text ?? '未记录'
  }
  const basis = value?.basis ?? clue?.scope?.time_basis
  return basis ? `${range}（时间依据：${basis}）` : range
}

function summaryOf(clue) {
  return clue?.summary ?? clue?.description ?? clue?.explanation ?? '系统根据已保存的公告关系生成待核查线索。'
}

function semanticsEntries(value) {
  if (Array.isArray(value)) return value
  if (!value || typeof value !== 'object') return []
  return Object.entries(value).map(([label, entry]) => entry && typeof entry === 'object'
    ? { ...entry, label: semanticLabel(entry) || label }
    : { label, text: entry })
}

function semanticLabel(entry) {
  if (entry == null) return ''
  if (typeof entry !== 'object') return String(entry)
  return entry.label ?? entry.name ?? entry.key ?? entry.title ?? ''
}

function semanticText(entry) {
  if (entry == null || typeof entry !== 'object') return ''
  const description = entry.text ?? entry.description ?? entry.value ?? entry.definition ?? ''
  const scopeLabels = {
    participation_rule: '参与口径',
    participation_outcomes: '参与结果',
    include_winners: '包含中标方',
    award_rule: '中标口径',
    project_deduplication: '项目去重',
    package_deduplication: '采购包口径',
    default_package_rule: '默认包号',
    time_basis: '时间依据',
  }
  const scope = entry.scope || {}
  const details = Object.entries(scopeLabels)
    .filter(([key]) => scope[key] != null && scope[key] !== '')
    .map(([key, label]) => {
      const value = Array.isArray(scope[key]) ? scope[key].join('、') : typeof scope[key] === 'boolean' ? (scope[key] ? '是' : '否') : scope[key]
      return `${label}：${value}`
    })
  return [typeof description === 'string' ? description : '', ...details].filter(Boolean).join('；')
}

function warningText(entry) {
  if (entry == null) return ''
  if (typeof entry !== 'object') return String(entry)
  return entry.message ?? entry.text ?? entry.description ?? entry.warning ?? entry.code ?? '数据质量提示'
}

function warningCode(entry) {
  if (entry == null || typeof entry !== 'object') return ''
  return String(entry.code ?? entry.kind ?? entry.type ?? '').toLowerCase()
}

function warningClass(entry) {
  const code = warningCode(entry)
  if (code.includes('ocr')) return 'warning-ocr'
  if (code.includes('attach') || code.includes('parse')) return 'warning-parse'
  if (code.includes('evidence') || code.includes('source')) return 'warning-evidence'
  if (code.includes('default') || code.includes('package')) return 'warning-package'
  return ''
}

function openNotice(entry, fallbackLink = null) {
  const link = entry && typeof entry === 'object' ? entry : (fallbackLink || {})
  const noticeId = noticeIdOf(link) ?? noticeIdOf(entry)
  if (noticeId == null || noticeId === '') return
  emit('open-notice', {
    noticeId,
    itemId: itemIdOf(link) ?? itemIdOf(entry),
    clue: link,
  })}

function clueKey(clue, index) {
  return clue?.id ?? clue?.key ?? (String(clue?.type || clue?.clue_type || 'clue') + '-' + index)
}

async function load() {
  const current = ++version
  loading.value = true
  error.value = ''
  try {
    const response = await datasetFetch(endpointUrl.value)
    const result = await response.json()
    if (!response.ok) throw new Error(result?.detail || '关系线索读取失败')
    payload.value = result || { clues: [] }
  } catch (cause) {
    if (cause.name === 'AbortError') return
    error.value = cause.message || '关系线索读取失败'
    payload.value = null
  } finally {
    if (current === version) loading.value = false
  }
}

watch(() => [props.datasetId, props.refreshKey, props.endpoint], () => {
  payload.value = null
  load()
}, { flush: 'sync' })
onMounted(load)
onBeforeUnmount(() => { version += 1 })
</script>

<template>  <section class="relationship-clues" aria-label="关系待核查线索">    <header class="relationship-clues-heading">      <div>        <p class="eyebrow">RELATIONSHIP REVIEW</p>        <h3>关系线索 · 待核查</h3>        <p class="relationship-clues-intro">线索用于安排人工核查，页面不作事实或责任判断。</p>      </div>      <button type="button" class="secondary" :disabled="loading" @click="load">        {{ loading ? '读取中…' : '刷新线索' }}      </button>    </header>    <p v-if="error" class="relationship-clues-error" role="alert">      {{ error }} <button type="button" class="secondary" @click="load">重试</button>    </p>    <p v-else-if="loading && !payload" class="relationship-clues-empty">正在读取关系线索…</p>    <template v-else>      <div v-if="scope && Object.keys(scope).length" class="relationship-clues-scope">        <span v-if="scope.notice_count != null">公告 {{ scope.notice_count }} 条</span>        <span v-if="scope.project_count != null">项目 {{ scope.project_count }} 个</span>        <span v-if="scope.package_count != null">采购包 {{ scope.package_count }} 个</span>        <span v-if="scope.time_range">时间范围：{{ typeof scope.time_range === 'object' ? timeRangeOf({ time_range: scope.time_range }) : scope.time_range }}</span>      </div>      <div v-if="clues.length" class="relationship-clues-list">        <article v-for="(clue, index) in clues" :key="clueKey(clue, index)" class="relationship-clue-card">          <header class="relationship-clue-card-heading">            <div>              <span class="relationship-clue-type">{{ typeLabel(clue) }}</span>              <h4>{{ clue.title || typeLabel(clue) }}</h4>            </div>            <span class="clue-status">待核查线索</span>          </header>          <p class="relationship-clue-summary">{{ summaryOf(clue) }}</p><div v-if="Object.keys(metricsOf(clue)).length" class="clue-metrics"><span v-for="(value, key) in metricsOf(clue)" :key="key"><strong>{{ value }}</strong><small>{{ key }}</small></span></div>          <div v-if="participantsOf(clue).length" class="clue-block">            <h5>涉及主体</h5>            <div class="clue-pills"><span v-for="(participant, participantIndex) in participantsOf(clue)" :key="`${participantLabel(participant)}-${participantIndex}`">{{ participantLabel(participant) }}</span></div>          </div>          <div class="clue-evidence-grid">            <div class="clue-block">              <h5>构成公告 <span>{{ noticesOf(clue).length }}</span></h5>              <ul v-if="noticesOf(clue).length">                <li v-for="(notice, noticeIndex) in noticesOf(clue)" :key="`${noticeIdOf(notice) ?? 'notice'}-${noticeIndex}`">                  <span>{{ noticeLabel(notice) }}</span>                  <button v-if="noticeIdOf(notice) != null" type="button" class="source-open" @click="openNotice(notice)">查看公告证据</button>                </li>              </ul>              <p v-else class="clue-muted">未记录</p>            </div>            <div class="clue-block">              <h5>构成项目 <span>{{ projectsOf(clue).length }}</span></h5>              <ul v-if="projectsOf(clue).length">                <li v-for="(project, projectIndex) in projectsOf(clue)" :key="`${projectIdOf(project) ?? 'project'}-${projectIndex}`">{{ projectLabel(project) }}</li>              </ul>              <p v-else class="clue-muted">未记录</p>            </div>            <div class="clue-block">              <h5>构成采购包 <span>{{ packagesOf(clue).length }}</span></h5>              <ul v-if="packagesOf(clue).length">                <li v-for="(pack, packageIndex) in packagesOf(clue)" :key="`${packageCodeOf(pack) ?? 'package'}-${packageIndex}`">                  <span>{{ packageLabel(pack) }}</span>                  <small v-if="String(packageCodeOf(pack)).toLowerCase() === 'default'" class="clue-warning-inline">包号来源不足</small>                </li>              </ul>              <p v-else class="clue-muted">未记录</p>            </div>            <div class="clue-block">              <h5>时间范围</h5>              <p>{{ timeRangeOf(clue) }}</p>            </div>          </div>          <div v-if="linksOf(clue).length" class="clue-links">            <h5>公告与证据入口</h5>            <button v-for="(link, linkIndex) in linksOf(clue)" :key="`${noticeIdOf(link) ?? 'link'}-${linkIndex}`" type="button" class="source-open" :disabled="noticeIdOf(link) == null" @click="openNotice(link)">              {{ link.label || link.title || '查看公告与证据' }}<span v-if="noticeIdOf(link) != null"> · 公告 #{{ noticeIdOf(link) }}</span>            </button>          </div>          <div v-if="warningsOf(clue).length" class="clue-warnings">            <h5>数据质量提示</h5>            <p v-for="(warning, warningIndex) in warningsOf(clue)" :key="`${warningText(warning)}-${warningIndex}`" :class="['clue-warning', warningClass(warning)]">{{ warningText(warning) }}</p>          </div>          <p v-else class="clue-no-warning">本条线索未返回 OCR、附件解析或证据不足提示。</p>        </article>      </div>      <p v-else class="relationship-clues-empty">当前数据集暂无可展示的待核查线索。</p>      <section v-if="semanticsEntries(definitions).length" class="relationship-clues-definitions">        <h4>统计口径与解释</h4>        <ul>          <li v-for="(definition, definitionIndex) in semanticsEntries(definitions)" :key="`${semanticLabel(definition)}-${definitionIndex}`">            <strong v-if="semanticLabel(definition)">{{ semanticLabel(definition) }}</strong>            <span>{{ semanticText(definition) || semanticLabel(definition) }}</span>          </li>        </ul>      </section>    </template>  </section>
</template>

<style scoped>.relationship-clues { margin-top: 1.25rem; border: 1px solid #d9e2dc; border-radius: 18px; background: #fff; overflow: hidden; }
.relationship-clues-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:1rem; padding:1.15rem 1.25rem; border-bottom:1px solid #edf1ee; }
.relationship-clues-heading h3 { margin:.1rem 0 .35rem; color:#29453a; font-size:1.05rem; }
.relationship-clues-intro { margin:0; color:#708077; font-size:.78rem; }
.relationship-clues-scope { display:flex; flex-wrap:wrap; gap:.45rem; padding:.75rem 1.25rem; border-bottom:1px solid #edf1ee; color:#557066; font-size:.75rem; }
.relationship-clues-scope span { padding:.25rem .55rem; border-radius:999px; background:#f1f6f2; }
.relationship-clues-list { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.8rem; padding:1rem 1.25rem; }
.relationship-clue-card { border:1px solid #e1e9e3; border-radius:12px; padding:1rem; background:#fcfdfc; min-width:0; }
.relationship-clue-card-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:.7rem; }
.relationship-clue-type { color:#0b7c60; font:10px var(--mono); letter-spacing:.04em; }
.relationship-clue-card h4 { margin:.25rem 0 0; color:#34433d; font-size:.94rem; }
.clue-status { flex:none; padding:.22rem .48rem; border-radius:999px; color:#775a13; background:#fff6d9; font-size:.68rem; }
.relationship-clue-summary { margin:.7rem 0; color:#5b6d64; font-size:.8rem; line-height:1.55; }
.clue-metrics { display:flex; flex-wrap:wrap; gap:.4rem; margin:.55rem 0 .8rem; }
.clue-metrics span { display:grid; gap:.1rem; padding:.35rem .5rem; border-radius:6px; background:#f1f5f2; color:#557066; }
.clue-metrics strong { color:#315b4c; font-size:.8rem; }
.clue-metrics small { font-size:.62rem; }
.clue-evidence-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.7rem; }
.clue-block { min-width:0; }
.clue-block h5,.clue-links h5,.clue-warnings h5 { margin:0 0 .35rem; color:#557066; font-size:.72rem; font-weight:650; }
.clue-block h5 span { color:#8a9891; font-weight:400; }
.clue-block p { margin:0; color:#45574f; font-size:.75rem; line-height:1.45; }
.clue-block ul { list-style:none; margin:0; padding:0; max-height:120px; overflow:auto; }
.clue-block li { display:flex; justify-content:space-between; align-items:flex-start; gap:.35rem; padding:.28rem 0; border-bottom:1px solid #eef2ef; color:#45574f; font-size:.74rem; line-height:1.4; }
.clue-block li:last-child { border-bottom:0; }
.clue-pills { display:flex; flex-wrap:wrap; gap:.3rem; }
.clue-pills span { padding:.25rem .45rem; border-radius:5px; background:#eef6f2; color:#356253; font-size:.74rem; }
.clue-muted,.clue-no-warning { color:#98a49e!important; font-size:.72rem!important; }
.clue-warning-inline { flex:none; color:#9a6a18; font-size:.66rem; }
.clue-links { margin-top:.75rem; padding-top:.65rem; border-top:1px solid #edf1ee; display:flex; flex-wrap:wrap; gap:.35rem .55rem; align-items:center; }
.clue-links h5 { flex-basis:100%; }
.clue-links .source-open { padding:.25rem .45rem; }
.clue-warnings { margin-top:.75rem; padding-top:.65rem; border-top:1px solid #edf1ee; }
.clue-warning { margin:.28rem 0 0; padding:.35rem .5rem; border-radius:5px; background:#fff8e9; color:#795b20; font-size:.71rem; line-height:1.4; }
.clue-warning.warning-ocr { background:#fff3e9; color:#8b551f; }
.clue-warning.warning-parse { background:#fff1ed; color:#884b3f; }
.clue-warning.warning-evidence { background:#f4f1fb; color:#65568f; }
.clue-warning.warning-package { background:#fff6d9; color:#775a13; }
.clue-no-warning { margin:.75rem 0 0; padding-top:.65rem; border-top:1px solid #edf1ee; }
.relationship-clues-definitions { margin:0 1.25rem 1.1rem; padding: .8rem .9rem; border-radius:8px; background:#f5f8f5; }
.relationship-clues-definitions h4 { margin:0 0 .4rem; color:#40584e; font-size:.78rem; }
.relationship-clues-definitions ul { list-style:none; margin:0; padding:0; }
.relationship-clues-definitions li { display:flex; gap:.35rem; margin:.25rem 0; color:#61736a; font-size:.72rem; line-height:1.45; }
.relationship-clues-definitions li strong { flex:none; color:#496458; }
.relationship-clues-empty { margin:0; padding:1.2rem 1.25rem; color:#87958e; font-size:.8rem; }
.relationship-clues-error { margin:0; padding:1rem 1.25rem; color:#ad4238; font-size:.8rem; }
@media (max-width:900px) { .relationship-clues-list { grid-template-columns:1fr; } }
@media (max-width:560px) { .relationship-clues-heading { display:block; } .relationship-clues-heading button { margin-top:.75rem; } .clue-evidence-grid { grid-template-columns:1fr; } }</style>
