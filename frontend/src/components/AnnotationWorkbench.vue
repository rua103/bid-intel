<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { annotationId } from '../utils/browser.js'

const props = defineProps({ apiBase: { type: String, required: true } })
const fields = [ ['product_name', '产品 / 服务'], ['category', '品目'], ['brand', '品牌'], ['model', '规格型号'], ['quantity', '数量'], ['unit_price', '单价（元）'], ['total_price', '总价（元）'] ]
const gold = ref(null)
const predictions = ref(null)
const sources = ref([])
const files = ref([])
const mode = ref('rules')
const activeNotice = ref(0)
const activePackage = ref(0)
const busy = ref(false)
const message = ref('')
const error = ref('')
const report = ref(null)
const markdown = ref('')
const reviewed = ref(false)
const confirmIdenticalGold = ref(false)
const notice = computed(() => gold.value?.notices?.[activeNotice.value])
const pkg = computed(() => notice.value?.packages?.[activePackage.value])
const source = computed(() => sources.value.find(s => s.notice_id === notice.value?.notice_id))
const metric = value => value == null ? 'N/A' : `${(value * 100).toFixed(2)}%`
const clone = value => JSON.parse(JSON.stringify(value))
const uid = annotationId
const storageKey = 'bid-intel-annotation-v1'

function resetReport() {
  report.value = null
  markdown.value = ''
  reviewed.value = false
  confirmIdenticalGold.value = false
}
function findItem(dataset, noticeId, packageId, itemId) {
  return dataset?.notices?.find(entry => entry.notice_id === noticeId)
    ?.packages?.find(entry => entry.package_id === packageId)
    ?.items?.find(entry => entry.item_id === itemId)
}
// Flattens the per-field verdicts the report already carries into rows a reviewer can act on,
// showing the human answer next to the system answer. Only wrong/missing/extra are listed;
// "correct" and "empty" are not errors.
const fieldErrors = computed(() => (report.value?.alignments || []).flatMap(alignment =>
  Object.entries(alignment.fields)
    .filter(([, status]) => ['wrong', 'missing', 'extra'].includes(status))
    .map(([field, status]) => ({
      noticeId: alignment.notice_id,
      packageId: alignment.package_id,
      field: fields.find(([key]) => key === field)?.[1] || field,
      status: { wrong: '值错误', missing: '漏提', extra: '多提' }[status],
      goldValue: findItem(gold.value, alignment.notice_id, alignment.package_id, alignment.gold_item_id)?.[field] ?? null,
      predictedValue: findItem(predictions.value, alignment.notice_id, alignment.package_id, alignment.predicted_item_id)?.[field] ?? null,
    }))
))
watch(activeNotice, () => { activePackage.value = 0 })
watch(gold, () => {
  if (gold.value) {
    resetReport()
    gold.value.status = 'draft'
    try { localStorage.setItem(storageKey, JSON.stringify({ gold: gold.value, predictions: predictions.value, sources: sources.value })) }
    catch { message.value = '浏览器草稿空间不足，请立即导出 gold JSON 保存。' }
  }
}, { deep: true })
// Predictions are persisted separately: loading a predictions JSON without touching gold
// should still survive a refresh.
watch(predictions, () => {
  resetReport()
  if (gold.value) {
    try { localStorage.setItem(storageKey, JSON.stringify({ gold: gold.value, predictions: predictions.value, sources: sources.value })) }
    catch { message.value = '浏览器草稿空间不足，请立即导出 JSON 保存。' }
  }
}, { deep: true })

onMounted(() => {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null')
    if (saved?.gold) { gold.value = saved.gold; predictions.value = saved.predictions; sources.value = saved.sources || []; message.value = '已恢复浏览器中的标注草稿。' }
  } catch { message.value = '旧草稿无法恢复，请导入之前导出的 JSON。' }
})

function download(filename, value, type = 'application/json') {
  const blob = new Blob([typeof value === 'string' ? value : JSON.stringify(value, null, 2)], { type })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function generate() {
  if (!files.value.length) return
  busy.value = true; error.value = ''; message.value = ''
  const body = new FormData()
  files.value.forEach(file => body.append('files', file))
  try {
    const response = await fetch(`${props.apiBase}/api/v1/evaluation/draft?mode=${mode.value}`, { method: 'POST', body })
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
    predictions.value = clone(data.predictions); sources.value = data.sources
    gold.value = clone(data.gold); activeNotice.value = 0; activePackage.value = 0
    message.value = `已生成 ${gold.value.notices.length} 条空白 gold 标注表；预测结果已单独保存。请对照原文独立填写 gold。${data.orphan_files.length ? ` ${data.orphan_files.length} 个附件未匹配，需单独处理。` : ''}`
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

async function loadJson(event, target) {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    const data = JSON.parse(await file.text())
    if (data.schema_version !== '1.0' || !Array.isArray(data.notices)) throw new Error('请使用 schema_version 为 1.0 的评测 JSON。')
    if (target === 'gold') {
      if (!['draft', 'reviewed'].includes(data.status)) throw new Error('请导入 gold，不是预测结果。')
      gold.value = data; sources.value = []; activeNotice.value = 0; activePackage.value = 0
    } else {
      if (data.status !== 'predicted') throw new Error('预测 JSON 的 status 应为 predicted。')
      predictions.value = data
    }
    resetReport(); message.value = `已导入 ${file.name}`; error.value = ''
  } catch (cause) { error.value = cause.message }
  event.target.value = ''
}

function addItem() {
  pkg.value.items.push({ item_id: uid('item'), ...Object.fromEntries(fields.map(([key]) => [key, null])) })
}
function addPerson(role) {
  const person = { entity_id: uid('entity'), name: '' }
  if (role === 'winners') person.award_amount = null
  else person.outcome = 'unknown'
  pkg.value[role].push(person)
}
function addPackage() {
  notice.value.packages.push({ package_id: uid('package'), buyer: null, items: [], winners: [], bidders: [] })
  activePackage.value = notice.value.packages.length - 1
}
function setBuyer(value) {
  pkg.value.buyer = value.trim() ? { entity_id: pkg.value.buyer?.entity_id || uid('buyer'), name: value } : null
}
function preparedGold() { return { ...clone(gold.value), status: reviewed.value ? 'reviewed' : 'draft' } }

async function evaluate() {
  busy.value = true; error.value = ''
  const body = new FormData()
  body.append('gold', new Blob([JSON.stringify(preparedGold())], { type: 'application/json' }), 'gold.json')
  body.append('predictions', new Blob([JSON.stringify(predictions.value)], { type: 'application/json' }), 'predictions.json')
  try {
    const query = confirmIdenticalGold.value ? '?allow_identical_gold=true' : ''
    const response = await fetch(`${props.apiBase}/api/v1/evaluation/run${query}`, { method: 'POST', body })
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
    report.value = data.report; markdown.value = data.markdown
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>

<template>
  <section id="annotation" class="panel annotation-panel">
    <div class="panel-heading"><div><span class="step">05</span><h3>人工标注与质量评测</h3></div><span class="hint">gold 与预测分开保存 · 修改后需重新核验</span></div>
    <p class="annotation-note">上传材料会分别生成空白 gold 标注表和自动预测结果。请对照原文独立填写 gold；空白表示原文未披露，金额统一为元。自动抽取结果不能直接作为人工标准答案。</p>
    <div class="annotation-toolbar">
      <label class="file-picker">选择标注材料<input type="file" multiple accept=".html,.htm,.zip,.docx,.xlsx,.pdf,.txt,.png,.jpg,.jpeg" @change="files = Array.from($event.target.files || [])" /></label>
      <select v-model="mode" aria-label="草稿抽取方案"><option value="rules">表格规则（不调用模型）</option><option value="model">纯模型（调用已配置 API）</option><option value="hybrid">规则 + 模型（调用已配置 API）</option></select>
      <button class="primary" :disabled="!files.length || busy" @click="generate">{{ busy ? '处理中…' : `生成草稿${files.length ? `（${files.length} 文件）` : ''}` }}</button>
      <label class="file-picker">导入 gold JSON<input type="file" accept=".json" @change="loadJson($event, 'gold')" /></label>
      <label class="file-picker">导入预测 JSON<input type="file" accept=".json" @change="loadJson($event, 'predictions')" /></label>
    </div>
    <p v-if="message" class="annotation-message" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>

    <div v-if="gold?.notices?.length" class="annotation-body">
      <div class="annotation-toolbar">
        <label>公告<select v-model="activeNotice"><option v-for="(entry, index) in gold.notices" :key="entry.notice_id" :value="index">{{ index + 1 }} / {{ gold.notices.length }} · {{ entry.notice_id }}</option></select></label>
        <label>采购包<select v-model="activePackage"><option v-for="(entry, index) in notice?.packages" :key="index" :value="index">{{ entry.package_id }}</option></select></label>
        <button class="secondary" @click="addPackage">增加采购包</button>
      </div>
      <div class="annotation-columns">
        <aside class="source-pane"><h4>来源原文</h4><p v-if="!source">当前 JSON 不含原文，请对照团队保存的原始公告。</p><p v-for="warning in source?.warnings || []" :key="warning" class="annotation-warning">{{ warning }}</p><pre>{{ source?.text || '上传原始材料后，这里显示提取到的原文。扫描件仍需人工打开原附件核对。' }}</pre></aside>
        <div v-if="pkg" class="edit-pane">
          <div class="annotation-toolbar"><label>包编号<input v-model="pkg.package_id" aria-label="包编号" /></label><label>采购单位<input :value="pkg.buyer?.name || ''" placeholder="原文未披露时留空" @input="setBuyer($event.target.value)" /></label></div>
          <div class="edit-heading"><h4>七字段标的物</h4><button class="secondary" @click="addItem">增加标的</button></div>
          <div class="table-wrap"><table class="annotation-table"><thead><tr><th v-for="[key, label] in fields" :key="key">{{ label }}</th><th>操作</th></tr></thead><tbody><tr v-for="(item, index) in pkg.items" :key="item.item_id"><td v-for="[key, label] in fields" :key="key"><input :value="item[key] ?? ''" :aria-label="`${label} ${index + 1}`" @input="item[key] = $event.target.value.trim() || null" /></td><td><button class="remove-row" :aria-label="`删除标的 ${index + 1}`" @click="pkg.items.splice(index, 1)">删除</button></td></tr><tr v-if="!pkg.items.length"><td colspan="8" class="empty">原文有标的物时请补录。</td></tr></tbody></table></div>
          <div class="edit-heading"><h4>中标方与各自中标金额</h4><button class="secondary" @click="addPerson('winners')">增加中标方</button></div>
          <div v-for="(entity, index) in pkg.winners" :key="entity.entity_id" class="entity-row"><input v-model="entity.name" aria-label="中标主体名称" placeholder="中标主体全称 / 联合体全称" /><input :value="entity.award_amount ?? ''" aria-label="中标金额" placeholder="明确披露的金额（元）" @input="entity.award_amount = $event.target.value.trim() || null" /><button class="remove-row" @click="pkg.winners.splice(index, 1)">删除</button></div>
          <div class="edit-heading"><h4>全部已确认投标主体（含中标方）</h4><button class="secondary" @click="addPerson('bidders')">增加投标方</button></div>
          <div v-for="(entity, index) in pkg.bidders" :key="entity.entity_id" class="entity-row"><input v-model="entity.name" aria-label="投标主体名称" placeholder="投标主体全称" /><select v-model="entity.outcome" aria-label="投标结果"><option value="winner">明确中标</option><option value="nonwinner">明确未中标</option><option value="unknown">结果未披露</option></select><button class="remove-row" @click="pkg.bidders.splice(index, 1)">删除</button></div>
          <p class="annotation-note">中标方应同时列入投标主体。联合体保留原文整体身份；不要把同一笔中标金额重复分配给各成员。</p>
        </div>
      </div>
      <label class="review-check"><input v-model="reviewed" type="checkbox" />我已对照原文独立填写并核验本文件全部 {{ gold.notices.length }} 条公告。</label>
      <label v-if="reviewed" class="review-check"><input v-model="confirmIdenticalGold" type="checkbox" />如果 gold 与预测完全一致，我已再次独立核对原文；允许报告完美匹配。</label>
      <div class="annotation-toolbar"><button class="secondary" @click="download(reviewed ? 'gold.reviewed.json' : 'gold.draft.json', preparedGold())">导出 {{ reviewed ? '已核验 gold' : 'gold 草稿' }}</button><button class="secondary" :disabled="!predictions" @click="download('predictions.json', predictions)">导出原始预测</button><button class="primary" :disabled="!reviewed || !predictions || busy" @click="evaluate">计算指标与报告</button></div>
    </div>
    <div v-if="report" class="evaluation-results">
      <p v-for="warning in report.warnings" :key="warning" class="annotation-warning">{{ warning }}</p>
      <p class="annotation-warning">以下为本地验证口径：准确率 = TP / (TP + FP + FN)，非官方评分。加权值 = 准确率 × 0.4 + 精确率 × 0.3 + 召回率 × 0.3。N/A 表示无可评分样本。</p>
      <div class="metric-grid"><div v-for="[key, label] in [['accuracy','字段准确率'],['precision','字段精确率'],['recall','字段召回率'],['f1','字段 F1'],['weighted_score','字段加权值']]" :key="key"><small>{{ label }}</small><strong>{{ metric(report.field_micro[key]) }}</strong></div></div>
      <div class="table-wrap"><table><thead><tr><th>粒度</th><th>TP / FP / FN</th><th>准确率</th><th>精确率</th><th>召回率</th><th>F1</th></tr></thead><tbody><tr v-for="[name, result] in Object.entries({ ...report.by_field, records: report.records, ...report.entities })" :key="name"><td>{{ fields.find(([key]) => key === name)?.[1] || name }}</td><td>{{ result.tp }} / {{ result.fp }} / {{ result.fn }}</td><td>{{ metric(result.accuracy) }}</td><td>{{ metric(result.precision) }}</td><td>{{ metric(result.recall) }}</td><td>{{ metric(result.f1) }}</td></tr></tbody></table></div>
      <h4>逐字段错误（{{ fieldErrors.length }}）</h4>
      <div v-if="fieldErrors.length" class="table-wrap"><table><thead><tr><th>公告 / 采购包</th><th>字段</th><th>错误类型</th><th>人工答案</th><th>系统答案</th></tr></thead><tbody><tr v-for="(entry, index) in fieldErrors" :key="index"><td>{{ entry.noticeId }} / {{ entry.packageId }}</td><td>{{ entry.field }}</td><td>{{ entry.status }}</td><td>{{ entry.goldValue ?? '—' }}</td><td>{{ entry.predictedValue ?? '—' }}</td></tr></tbody></table></div>
      <p v-else class="annotation-note">没有发现标的物七字段错误。实体差异请结合上表的实体指标核对原文。</p>
      <div class="annotation-toolbar"><button class="secondary" @click="download('evaluation-report.json', report)">导出报告 JSON</button><button class="secondary" @click="download('evaluation-report.md', markdown, 'text/markdown')">导出 Markdown 报告</button></div>
    </div>
  </section>
</template>

<style scoped>
.annotation-note{font-size:12px;color:#6c7972;line-height:1.8}.annotation-toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:10px;margin:14px 0}.annotation-toolbar label{display:flex;flex-direction:column;gap:5px;font-size:11px;color:#587065}.annotation-panel input:not([type=checkbox]),.annotation-panel select{border:1px solid #d6e1d9;border-radius:5px;padding:9px;color:#243c30;background:#fff;font:12px inherit;max-width:100%}.annotation-panel button:disabled{opacity:.5;cursor:not-allowed}.file-picker{position:relative;border:1px solid #d6e1d9;border-radius:6px;padding:10px 12px;overflow:hidden;cursor:pointer}.file-picker input{position:absolute;inset:0;opacity:0;cursor:pointer}.annotation-message{font-size:12px;color:#087b61}.annotation-columns{display:grid;grid-template-columns:minmax(220px,.7fr) minmax(0,1.5fr);gap:20px;border-top:1px solid #e3e7e3;padding-top:15px}.source-pane{min-width:0;max-height:680px;overflow:auto;background:#f6f8f6;border-radius:8px;padding:14px}.source-pane pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.85 inherit}.source-pane h4,.edit-pane h4{font-size:12px;color:#32503e;margin:6px 0}.source-pane p{font-size:11px;line-height:1.7}.annotation-warning{font-size:11px;color:#88632d;line-height:1.7;background:#fff8e9;padding:9px 12px;border-radius:6px}.edit-heading{display:flex;align-items:center;justify-content:space-between;margin:15px 0 10px}.annotation-table{min-width:850px}.annotation-table td{padding:6px}.annotation-table input{width:115px}.remove-row{border:0;color:#a55247;background:transparent;cursor:pointer;font-size:11px}.entity-row{display:grid;grid-template-columns:1.6fr 1fr auto;gap:8px;margin:8px 0}.entity-row input{min-width:0}.review-check{display:flex;align-items:center;gap:8px;font-size:12px;color:#365444;margin:22px 0 12px;line-height:1.8}.metric-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}.metric-grid>div{background:#edf6f0;padding:14px;border-radius:7px}.metric-grid small{display:block;font-size:11px;color:#5d806a;margin-bottom:9px}.metric-grid strong{font-size:22px;color:#126b4d}.evaluation-results{border-top:1px solid #e3e7e3;margin-top:20px;padding-top:15px}@media(max-width:900px){.annotation-columns{grid-template-columns:1fr}.source-pane{max-height:240px}.metric-grid{grid-template-columns:repeat(2,1fr)}.annotation-toolbar{align-items:stretch}.entity-row{grid-template-columns:1fr 1fr auto}}
</style>
