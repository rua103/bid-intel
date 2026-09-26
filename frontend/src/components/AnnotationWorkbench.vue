<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { annotationId } from '../utils/browser.js'
import {
  ANNOTATION_TASK_SCHEMA,
  annotationNoticeIssues,
  annotationNoticeReviewed,
  annotationNoticeSignature,
  annotationProgress,
  annotationTaskKey,
  validateAnnotationTask,
} from '../utils/annotation.js'
import {
  LEGACY_ANNOTATION_SESSION_KEY,
  lastAnnotationSessionKey,
  loadAnnotationSession,
  saveAnnotationSession,
} from '../utils/annotationStorage.js'

const props = defineProps({ apiBase: { type: String, required: true }, teamMode: { type: Boolean, default: false } })
const fields = [ ['product_name', '产品 / 服务'], ['category', '品目'], ['brand', '品牌'], ['model', '规格型号'], ['quantity', '数量'], ['unit_price', '单价（元）'], ['total_price', '总价（元）'] ]
const gold = ref(null)
const predictions = ref(null)
const sources = ref([])
const files = ref([])
const mode = ref('rules')
const taskInfo = ref(null)
const verifiedSnapshots = ref({})
const sessionKey = ref(LEGACY_ANNOTATION_SESSION_KEY)
const activeNotice = ref(0)
const activePackage = ref(0)
const busy = ref(false)
const message = ref('')
const error = ref('')
const report = ref(null)
const markdown = ref('')
const confirmIdenticalGold = ref(false)
const notice = computed(() => gold.value?.notices?.[activeNotice.value])
const pkg = computed(() => notice.value?.packages?.[activePackage.value])
const source = computed(() => sources.value.find(s => s.notice_id === notice.value?.notice_id))
const progress = computed(() => annotationProgress(gold.value, verifiedSnapshots.value))
const currentNoticeIssues = computed(() => annotationNoticeIssues(notice.value))
const currentNoticeReviewed = computed(() => annotationNoticeReviewed(notice.value, verifiedSnapshots.value))
const sourceFiles = computed(() => (source.value?.files || []).map(file =>
  typeof file === 'string' ? { name: file, relative_path: '' } : file
))
const metric = value => value == null ? 'N/A' : `${(value * 100).toFixed(2)}%`
const clone = value => JSON.parse(JSON.stringify(value))
const uid = annotationId
let restoring = false
let persistTimer = null

function resetReport() {
  report.value = null
  markdown.value = ''
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
function sessionPayload() {
  return {
    gold: gold.value,
    predictions: predictions.value,
    sources: sources.value,
    taskInfo: taskInfo.value,
    verifiedSnapshots: verifiedSnapshots.value,
  }
}

watch([gold, predictions, sources, taskInfo, verifiedSnapshots], () => {
  if (restoring) return
  resetReport()
  if (gold.value?.status !== 'draft') gold.value.status = 'draft'
  clearTimeout(persistTimer)
  persistTimer = setTimeout(async () => {
    try {
      await saveAnnotationSession(sessionKey.value, sessionPayload())
      message.value = '草稿已自动保存到当前浏览器。交接或退出前请下载进度备份。'
    } catch (cause) {
      message.value = cause.message || '自动保存失败，请立即下载进度备份。'
    }
  }, 350)
}, { deep: true })

onMounted(async () => {
  restoring = true
  const key = lastAnnotationSessionKey()
  sessionKey.value = key
  try {
    const saved = await loadAnnotationSession(key)
    if (saved?.gold) {
      gold.value = saved.gold
      predictions.value = saved.predictions || null
      sources.value = saved.sources || []
      taskInfo.value = saved.taskInfo || null
      verifiedSnapshots.value = saved.verifiedSnapshots || {}
      message.value = '已恢复上次自动保存的标注进度。'
    } else if (key === LEGACY_ANNOTATION_SESSION_KEY) {
      message.value = '导入协调员发给你的标注任务包即可开始。'
    }
  } catch {
    message.value = '旧草稿无法恢复；请导入任务包或之前下载的进度备份。'
  }
  restoring = false
})

function download(filename, value, type = 'application/json') {
  const blob = new Blob([typeof value === 'string' ? value : JSON.stringify(value, null, 2)], { type })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function taskFileName(suffix) {
  const taskId = String(taskInfo.value?.task_id || 'annotation').replace(/[^A-Za-z0-9_-]/g, '-')
  return `${taskId}.${suffix}`
}

async function generate() {
  if (!files.value.length) return
  busy.value = true; error.value = ''; message.value = ''
  const body = new FormData()
  files.value.forEach(file => body.append('files', file))
  try {
    const response = await fetch(`${props.apiBase}/api/v1/evaluation/draft?mode=${mode.value}`, { method: 'POST', body, credentials: 'include' })
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
    sessionKey.value = LEGACY_ANNOTATION_SESSION_KEY
    taskInfo.value = null; verifiedSnapshots.value = {}
    predictions.value = clone(data.predictions); sources.value = data.sources
    gold.value = clone(data.gold); activeNotice.value = 0; activePackage.value = 0
    message.value = `已生成 ${gold.value.notices.length} 条空白 gold 标注表；预测结果已单独保存。请对照原文独立填写 gold。${data.orphan_files.length ? ` ${data.orphan_files.length} 个附件未匹配，需单独处理。` : ''}`
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

function taskSession(bundle) {
  return {
    gold: clone(bundle.gold),
    predictions: clone(bundle.predictions),
    sources: clone(bundle.sources),
    taskInfo: { task_id: bundle.task_id, assignee: bundle.assignee || '未命名任务' },
    verifiedSnapshots: clone(bundle.verified_snapshots || {}),
  }
}

async function loadTaskBundle(event) {
  const file = event.target.files?.[0]
  if (!file) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const bundle = validateAnnotationTask(JSON.parse(await file.text()))
    const key = annotationTaskKey(bundle.task_id)
    const existing = bundle.bundle_kind === 'resume' ? null : await loadAnnotationSession(key)
    const session = existing?.gold ? existing : taskSession(bundle)
    restoring = true
    sessionKey.value = key
    gold.value = session.gold
    predictions.value = session.predictions
    sources.value = session.sources || []
    taskInfo.value = session.taskInfo || { task_id: bundle.task_id, assignee: bundle.assignee || '标注任务' }
    verifiedSnapshots.value = session.verifiedSnapshots || {}
    activeNotice.value = 0; activePackage.value = 0
    resetReport()
    restoring = false
    await saveAnnotationSession(key, sessionPayload())
    message.value = existing?.gold
      ? `已恢复“${bundle.assignee || bundle.task_id}”的浏览器进度。`
      : `已载入 ${gold.value.notices.length} 条分配给你的公告；原文和预测已配好。`
  } catch (cause) {
    restoring = false
    error.value = cause.message
  } finally {
    busy.value = false
    event.target.value = ''
  }
}

async function loadJson(event, target) {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    const data = JSON.parse(await file.text())
    if (data.schema_version !== '1.0' || !Array.isArray(data.notices)) throw new Error('请使用 schema_version 为 1.0 的评测 JSON。')
    if (target === 'gold') {
      if (!['draft', 'reviewed'].includes(data.status)) throw new Error('请导入 gold，不是预测结果。')
      taskInfo.value = null; verifiedSnapshots.value = {}; sessionKey.value = LEGACY_ANNOTATION_SESSION_KEY
      gold.value = data; sources.value = []; activeNotice.value = 0; activePackage.value = 0
    } else {
      if (data.status !== 'predicted') throw new Error('预测 JSON 的 status 应为 predicted。')
      predictions.value = data
    }
    resetReport(); message.value = `已导入 ${file.name}`; error.value = ''
  } catch (cause) { error.value = cause.message }
  event.target.value = ''
}

function markCurrentNoticeReviewed(event) {
  const id = notice.value?.notice_id
  if (!id) return
  if (event.target.checked && currentNoticeIssues.value.length) {
    event.target.checked = false
    return
  }
  const updated = { ...verifiedSnapshots.value }
  if (event.target.checked) updated[id] = annotationNoticeSignature(notice.value)
  else delete updated[id]
  verifiedSnapshots.value = updated
}

function moveToNextUnreviewed() {
  if (!gold.value?.notices?.length) return
  for (let offset = 1; offset <= gold.value.notices.length; offset += 1) {
    const next = (activeNotice.value + offset) % gold.value.notices.length
    if (!annotationNoticeReviewed(gold.value.notices[next], verifiedSnapshots.value)) {
      activeNotice.value = next
      return
    }
  }
  message.value = '全部公告都已逐条核验；请导出已核验 Gold 和进度备份。'
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
function removePackage() {
  const packages = notice.value?.packages || []
  if (packages.length <= 1) {
    message.value = '至少保留一个包。原文确实未分包时，把包号填写为 default。'
    return
  }
  const selected = packages[activePackage.value]
  const hasAnnotations = Boolean(selected?.buyer || selected?.items?.length || selected?.winners?.length || selected?.bidders?.length)
  if (hasAnnotations && !window.confirm('这个包已经有标注内容。删除后该包内的标的和主体都会一起删除，确定继续吗？')) return
  packages.splice(activePackage.value, 1)
  activePackage.value = Math.min(activePackage.value, packages.length - 1)
}
function setBuyer(value) {
  pkg.value.buyer = value.trim() ? { entity_id: pkg.value.buyer?.entity_id || uid('buyer'), name: value } : null
}
function preparedGold(asReviewed = false, reviewedOnly = false) {
  const prepared = clone(gold.value)
  if (reviewedOnly) {
    prepared.notices = prepared.notices.filter(entry => annotationNoticeReviewed(entry, verifiedSnapshots.value))
  }
  return { ...prepared, status: asReviewed && prepared.notices.length > 0 ? 'reviewed' : 'draft' }
}

function exportProgress() {
  if (!gold.value || !predictions.value) return
  download(taskFileName('progress.backup.json'), {
    schema_version: ANNOTATION_TASK_SCHEMA,
    bundle_kind: 'resume',
    task_id: taskInfo.value?.task_id || 'manual',
    assignee: taskInfo.value?.assignee || '本地标注',
    gold: preparedGold(false),
    predictions: clone(predictions.value),
    sources: clone(sources.value),
    verified_snapshots: clone(verifiedSnapshots.value),
  })
}

async function evaluate() {
  busy.value = true; error.value = ''
  const body = new FormData()
  body.append('gold', new Blob([JSON.stringify(preparedGold(progress.value.complete))], { type: 'application/json' }), 'gold.json')
  body.append('predictions', new Blob([JSON.stringify(predictions.value)], { type: 'application/json' }), 'predictions.json')
  try {
    const query = confirmIdenticalGold.value ? '?allow_identical_gold=true' : ''
    const response = await fetch(`${props.apiBase}/api/v1/evaluation/run${query}`, { method: 'POST', body, credentials: 'include' })
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
    report.value = data.report; markdown.value = data.markdown
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>

<template>
  <section id="annotation" class="panel annotation-panel">
    <div class="panel-heading"><div><span class="step">{{ teamMode ? '任务' : '05' }}</span><h3>{{ teamMode ? '人工标注任务' : '人工标注与质量评测' }}</h3></div><span class="hint">任务按人隔离 · 每条单独核验 · 自动保存</span></div>
    <p class="annotation-note">队友操作：载入协调员发来的任务包 → 对照左侧原文逐条填写 → 勾选每条公告的核验框 → 下载已核验 Gold 和进度备份。系统预测只能作线索，不能照抄。</p>
    <div class="annotation-toolbar assignment-toolbar">
      <label class="file-picker">载入我的标注任务<input type="file" accept=".json" @change="loadTaskBundle" /></label>
      <button class="secondary" :disabled="!gold || !predictions" @click="exportProgress">下载进度备份</button>
    </div>
    <p v-if="taskInfo" class="task-progress"><strong>{{ taskInfo.assignee }}</strong> · 已核验 {{ progress.reviewed }} / {{ progress.total }} 条</p>
    <details v-if="!teamMode" class="manual-tools"><summary>备用：没有任务包时，手动上传原始公告或 JSON</summary>
      <div class="annotation-toolbar">
        <label class="file-picker">选择原始公告<input type="file" multiple accept=".html,.htm,.zip,.doc,.docx,.xls,.xlsx,.pdf,.txt,.png,.jpg,.jpeg" @change="files = Array.from($event.target.files || [])" /></label>
        <select v-model="mode" aria-label="草稿抽取方案"><option value="rules">表格规则（不调用模型）</option><option value="model">纯模型（调用已配置 API）</option><option value="hybrid">规则 + 模型（调用已配置 API）</option></select>
        <button class="primary" :disabled="!files.length || busy" @click="generate">{{ busy ? '处理中…' : `生成草稿${files.length ? `（${files.length} 文件）` : ''}` }}</button>
        <label class="file-picker">导入 gold JSON<input type="file" accept=".json" @change="loadJson($event, 'gold')" /></label>
        <label class="file-picker">导入预测 JSON<input type="file" accept=".json" @change="loadJson($event, 'predictions')" /></label>
      </div>
    </details>
    <p v-if="message" class="annotation-message" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>

    <div v-if="gold?.notices?.length" class="annotation-body">
      <div class="annotation-toolbar">
        <label>公告<select v-model="activeNotice"><option v-for="(entry, index) in gold.notices" :key="entry.notice_id" :value="index">{{ annotationNoticeReviewed(entry, verifiedSnapshots) ? '✓ 已核验' : '○ 待核验' }} · {{ index + 1 }} / {{ gold.notices.length }} · {{ entry.notice_id }}</option></select></label>
        <label>采购包<select v-model="activePackage"><option v-for="(entry, index) in notice?.packages" :key="index" :value="index">{{ entry.package_id }}</option></select></label>
        <button class="secondary" @click="addPackage">补加原文中的包</button>
        <button class="remove-row" @click="removePackage">删除当前包</button>
        <button class="secondary" @click="moveToNextUnreviewed">下一条待核验</button>
      </div>
      <div class="annotation-columns">
        <aside class="source-pane"><h4>来源原文</h4><p v-if="!source">当前任务没有解析出的原文；请打开原附件核对，不能仅凭预测或空白完成标注。</p><p v-for="warning in source?.warnings || []" :key="warning" class="annotation-warning">{{ warning }}</p><div v-if="sourceFiles.length" class="source-files"><strong>原始文件（扫描件请打开附件核验）</strong><div v-for="file in sourceFiles" :key="file.relative_path || file.name"><span>{{ file.name }}</span><small>{{ file.relative_path }}</small></div></div><pre>{{ source?.text || '原文文字无法自动读取。请打开上方原始文件并核对；材料损坏或无法打开时，暂停该条并联系协调员。' }}</pre></aside>
        <div v-if="pkg" class="edit-pane">
          <div class="annotation-toolbar"><label>包号（照原文填写）<input v-model="pkg.package_id" aria-label="包号（照原文填写）" placeholder="未分包填 default" /></label><label>采购单位<input :value="pkg.buyer?.name || ''" placeholder="原文未披露时留空" @input="setBuyer($event.target.value)" /></label></div>
          <div class="edit-heading"><h4>七字段标的物</h4><button class="secondary" @click="addItem">增加标的</button></div>
          <div class="table-wrap"><table class="annotation-table"><thead><tr><th v-for="[key, label] in fields" :key="key">{{ label }}</th><th>操作</th></tr></thead><tbody><tr v-for="(item, index) in pkg.items" :key="item.item_id"><td v-for="[key, label] in fields" :key="key"><input :value="item[key] ?? ''" :aria-label="`${label} ${index + 1}`" @input="item[key] = $event.target.value.trim() || null" /></td><td><button class="remove-row" :aria-label="`删除标的 ${index + 1}`" @click="pkg.items.splice(index, 1)">删除</button></td></tr><tr v-if="!pkg.items.length"><td colspan="8" class="empty">原文有标的物时请补录。</td></tr></tbody></table></div>
          <div class="edit-heading"><h4>中标方与各自中标金额</h4><button class="secondary" @click="addPerson('winners')">增加中标方</button></div>
          <div v-for="(entity, index) in pkg.winners" :key="entity.entity_id" class="entity-row"><input v-model="entity.name" aria-label="中标主体名称" placeholder="中标主体全称 / 联合体全称" /><input :value="entity.award_amount ?? ''" aria-label="中标金额" placeholder="明确披露的金额（元）" @input="entity.award_amount = $event.target.value.trim() || null" /><button class="remove-row" @click="pkg.winners.splice(index, 1)">删除</button></div>
          <div class="edit-heading"><h4>全部已确认投标主体（含中标方）</h4><button class="secondary" @click="addPerson('bidders')">增加投标方</button></div>
          <div v-for="(entity, index) in pkg.bidders" :key="entity.entity_id" class="entity-row"><input v-model="entity.name" aria-label="投标主体名称" placeholder="投标主体全称" /><select v-model="entity.outcome" aria-label="投标结果"><option value="winner">明确中标</option><option value="nonwinner">明确未中标</option><option value="unknown">结果未披露</option></select><button class="remove-row" @click="pkg.bidders.splice(index, 1)">删除</button></div>
          <p class="annotation-note">中标方应同时列入投标主体。联合体保留原文整体身份；不要把同一笔中标金额重复分配给各成员。</p>
        </div>
      </div>
      <ul v-if="currentNoticeIssues.length" class="annotation-validation-errors"><li v-for="issue in currentNoticeIssues" :key="issue">{{ issue }}</li></ul>
      <label class="review-check"><input :checked="currentNoticeReviewed" :disabled="currentNoticeIssues.length > 0" type="checkbox" @change="markCurrentNoticeReviewed" />本公告已对照原文：逐包检查七字段、采购单位、中标方和投标方；未披露留空；扫描件已打开原附件核验。</label>
      <p class="annotation-note">修改本公告后，核验状态会自动取消。无法读取、附件缺失或包号有疑问时先不要勾选，联系协调员。</p>
      <label v-if="progress.complete" class="review-check"><input v-model="confirmIdenticalGold" type="checkbox" />如果 Gold 与系统预测完全相同，我已再次独立核对原文，允许生成完美匹配报告。</label>
      <div class="annotation-toolbar"><button class="secondary" :disabled="!progress.reviewed" @click="download(taskFileName(progress.complete ? 'gold.reviewed.json' : 'gold.reviewed.partial.json'), preparedGold(true, true))">导出已核验 Gold（{{ progress.reviewed }}/{{ progress.total }}）</button><button v-if="!teamMode" class="secondary" @click="download(taskFileName('gold.draft.json'), preparedGold(false))">导出 Gold 草稿</button><button v-if="!teamMode" class="secondary" :disabled="!predictions" @click="download(taskFileName('predictions.json'), predictions)">导出本任务预测</button><button v-if="!teamMode" class="primary" :disabled="!progress.complete || !predictions || busy" @click="evaluate">计算指标与报告</button></div>
    </div>
    <div v-if="report && !teamMode" class="evaluation-results">
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
.annotation-validation-errors{font-size:12px;color:#9d392c;background:#fff0ed;border-radius:6px;padding:10px 16px 10px 28px;line-height:1.7}
.annotation-note{font-size:12px;color:#6c7972;line-height:1.8}.annotation-toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:10px;margin:14px 0}.annotation-toolbar label{display:flex;flex-direction:column;gap:5px;font-size:11px;color:#587065}.annotation-panel input:not([type=checkbox]),.annotation-panel select{border:1px solid #d6e1d9;border-radius:5px;padding:9px;color:#243c30;background:#fff;font:12px inherit;max-width:100%}.annotation-panel button:disabled{opacity:.5;cursor:not-allowed}.file-picker{position:relative;border:1px solid #d6e1d9;border-radius:6px;padding:10px 12px;overflow:hidden;cursor:pointer}.file-picker input{position:absolute;inset:0;opacity:0;cursor:pointer}.annotation-message{font-size:12px;color:#087b61}.task-progress{background:#edf6f0;padding:10px 12px;border-radius:6px;color:#126b4d;font-size:12px}.manual-tools{margin:10px 0;color:#6c7972;font-size:12px}.manual-tools summary{cursor:pointer}.annotation-columns{display:grid;grid-template-columns:minmax(220px,.7fr) minmax(0,1.5fr);gap:20px;border-top:1px solid #e3e7e3;padding-top:15px}.source-pane{min-width:0;max-height:680px;overflow:auto;background:#f6f8f6;border-radius:8px;padding:14px}.source-pane pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.85 inherit}.source-pane h4,.edit-pane h4{font-size:12px;color:#32503e;margin:6px 0}.source-pane p{font-size:11px;line-height:1.7}.source-files{font-size:11px;color:#365444;background:#fff;border-radius:6px;padding:9px;margin:9px 0}.source-files>strong{display:block;margin-bottom:5px}.source-files div{display:grid;gap:2px;border-top:1px solid #eef0ee;padding:5px 0}.source-files small{color:#79837d;overflow-wrap:anywhere}.annotation-warning{font-size:11px;color:#88632d;line-height:1.7;background:#fff8e9;padding:9px 12px;border-radius:6px}.edit-heading{display:flex;align-items:center;justify-content:space-between;margin:15px 0 10px}.annotation-table{min-width:850px}.annotation-table td{padding:6px}.annotation-table input{width:115px}.remove-row{border:0;color:#a55247;background:transparent;cursor:pointer;font-size:11px}.entity-row{display:grid;grid-template-columns:1.6fr 1fr auto;gap:8px;margin:8px 0}.entity-row input{min-width:0}.review-check{display:flex;align-items:center;gap:8px;font-size:12px;color:#365444;margin:22px 0 12px;line-height:1.8}.metric-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0}.metric-grid>div{background:#edf6f0;padding:14px;border-radius:7px}.metric-grid small{display:block;font-size:11px;color:#5d806a;margin-bottom:9px}.metric-grid strong{font-size:22px;color:#126b4d}.evaluation-results{border-top:1px solid #e3e7e3;margin-top:20px;padding-top:15px}@media(max-width:900px){.annotation-columns{grid-template-columns:1fr}.source-pane{max-height:240px}.metric-grid{grid-template-columns:repeat(2,1fr)}.annotation-toolbar{align-items:stretch}.entity-row{grid-template-columns:1fr 1fr auto}}
</style>
