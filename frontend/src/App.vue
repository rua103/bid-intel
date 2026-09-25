<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import AnnotationWorkbench from './components/AnnotationWorkbench.vue'
import RelationshipGraph from './components/RelationshipGraph.vue'
import { resolveApiBase } from './utils/browser.js'
import { createDatasetClient } from './utils/datasets.js'

const apiBase = resolveApiBase(import.meta.env.VITE_API_BASE, window.location)
const selectedFiles = ref([])
const uploading = ref(false)
const notice = ref(null)
const batchResult = ref(null)
const error = ref('')
const health = ref(null)
const datasets = ref([])
const datasetId = ref('default')
const datasetName = ref('')
const datasetError = ref('')
const datasetReady = ref(false)
const datasetCreating = ref(false)
const capabilities = ref(null)
let datasetVersion = 0
const datasetFetch = createDatasetClient(() => datasetId.value, () => datasetVersion)
const searchText = ref('')
const searchBrand = ref('')
const searchCategory = ref('')
const items = ref([])
const searched = ref(false)
const organizations = ref([])
const selectedBuyerId = ref('')
const selectedSupplierId = ref('')
const selectedSupplierIds = ref([])
const analyticsResults = ref({})
const analyticsLoading = ref('')
const analyticsError = ref('')
const modelConfig = ref({ model_base_url: '', model_name: '', model_api_key_masked: '', api_key_configured: false, configured: false })
const modelKeyInput = ref('')
const modelSaving = ref(false)
const modelTesting = ref(false)
const modelMessage = ref('')
const modelMessageIsError = ref(false)
const apiKeyPlaceholder = computed(() => modelConfig.value.api_key_configured ? `已配置（${modelConfig.value.model_api_key_masked}）留空保持不变` : 'sk-...')
const prettyAmount = (value) => value == null ? '—' : Number(value).toLocaleString('zh-CN')

const statusText = computed(() => {
  if (!health.value) return '等待连接后端'
  return `已导入 ${health.value.notices_imported} 条公告`
})

async function refreshHealth() {
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/health`)
    if (!response.ok) throw new Error('后端暂不可用')
    health.value = await response.json()
  } catch (cause) {
    if (cause.name === 'AbortError') return
    health.value = null
  }
}

function onFileChange(event) {
  selectedFiles.value = Array.from(event.target.files || [])
  notice.value = null
  batchResult.value = null
  error.value = ''
}

async function importNotice() {
  if (!selectedFiles.value.length) return
  uploading.value = true
  error.value = ''
  notice.value = null
  const body = new FormData()
  for (const file of selectedFiles.value) body.append('files', file)
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/notices/import`, { method: 'POST', body })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '导入失败')
    notice.value = payload
    selectedFiles.value = []
    await refreshHealth()
    await searchItems()
    await refreshOrganizations()
  } catch (cause) {
    if (cause.name === 'AbortError') return
    error.value = cause.message || '导入失败，请检查文件和后端服务'
  } finally {
    uploading.value = false
  }
}

async function importBatch() {
  if (!selectedFiles.value.length) return
  uploading.value = true
  error.value = ''
  notice.value = null
  batchResult.value = null
  const body = new FormData()
  for (const file of selectedFiles.value) body.append('files', file)
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/notices/import-batch`, { method: 'POST', body })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '批量导入失败')
    batchResult.value = payload
    selectedFiles.value = []
    await refreshHealth()
    await searchItems()
    await refreshOrganizations()
  } catch (cause) {
    if (cause.name === 'AbortError') return
    error.value = cause.message || '批量导入失败，请检查文件和后端服务'
  } finally {
    uploading.value = false
  }
}

async function searchItems() {
  const query = new URLSearchParams()
  if (searchText.value.trim()) query.set('query', searchText.value.trim())
  if (searchBrand.value.trim()) query.set('brand', searchBrand.value.trim())
  if (searchCategory.value.trim()) query.set('category', searchCategory.value.trim())
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/items?${query}`)
    if (!response.ok) throw new Error('检索失败')
    items.value = await response.json()
    searched.value = true
  } catch (cause) {
    if (cause.name === 'AbortError') return
    error.value = cause.message
  }
}

async function refreshOrganizations() {
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/organizations?limit=500`)
    if (!response.ok) throw new Error('主体列表读取失败')
    organizations.value = await response.json()
  } catch (cause) {
    if (cause.name === 'AbortError') return
    analyticsError.value = cause.message
  }
}

async function loadModelConfig() {
  try {
    const response = await fetch(`${apiBase}/api/v1/model-config`)
    if (!response.ok) throw new Error('读取模型配置失败')
    modelConfig.value = await response.json()
  } catch (cause) {
    modelMessage.value = cause.message
    modelMessageIsError.value = true
  }
}

async function saveModelConfig() {
  modelSaving.value = true
  modelMessage.value = ''
  try {
    const response = await fetch(`${apiBase}/api/v1/model-config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model_base_url: modelConfig.value.model_base_url,
        model_api_key: modelKeyInput.value,
        model_name: modelConfig.value.model_name,
      }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '保存失败')
    modelConfig.value = payload
    modelKeyInput.value = ''
    modelMessage.value = payload.configured
      ? `已保存并启用模型 ${payload.model_name}`
      : '已保存，但配置不完整（接口地址、Key、模型名均不能为空）'
    modelMessageIsError.value = !payload.configured
  } catch (cause) {
    modelMessage.value = cause.message || '保存失败'
    modelMessageIsError.value = true
  } finally {
    modelSaving.value = false
  }
}

async function testModelConfig() {
  modelTesting.value = true
  modelMessage.value = ''
  try {
    const response = await fetch(`${apiBase}/api/v1/model-config/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model_base_url: modelConfig.value.model_base_url,
        model_api_key: modelKeyInput.value,
        model_name: modelConfig.value.model_name,
      }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '测试失败')
    modelMessage.value = payload.message
    modelMessageIsError.value = !payload.ok
  } catch (cause) {
    modelMessage.value = cause.message || '测试失败'
    modelMessageIsError.value = true
  } finally {
    modelTesting.value = false
  }
}

async function runScene(scene) {
  const version = datasetVersion
  analyticsLoading.value = scene
  analyticsError.value = ''
  let url = ''
  let options = {}
  if (scene === 'awardees') url = `${apiBase}/api/v1/analytics/buyers/${selectedBuyerId.value}/awardees`
  if (scene === 'bidders') url = `${apiBase}/api/v1/analytics/buyers/${selectedBuyerId.value}/bidders`
  if (scene === 'coBidders') url = `${apiBase}/api/v1/analytics/suppliers/${selectedSupplierId.value}/co-bidders`
  if (scene === 'commonBuyers') url = `${apiBase}/api/v1/analytics/common-buyers`
  if (scene === 'commonProjects') url = `${apiBase}/api/v1/analytics/common-projects`
  if (scene === 'commonBuyers' || scene === 'commonProjects') {
    options = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ organization_ids: selectedSupplierIds.value.map(Number) }),
    }
  }
  try {
    const response = await datasetFetch(url, options)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '查询失败')
    analyticsResults.value = { ...analyticsResults.value, [scene]: payload }
  } catch (cause) {
    if (cause.name === 'AbortError') return
    analyticsError.value = cause.message || '关系查询失败'
  } finally {
    if (version === datasetVersion) analyticsLoading.value = ''
  }
}

async function refreshDatasets() {
  const response = await fetch(apiBase + '/api/v1/datasets')
  if (!response.ok) throw new Error('数据集列表读取失败')
  datasets.value = await response.json()
}

async function createDataset() {
  if (!datasetName.value.trim() || uploading.value) return
  datasetCreating.value = true
  datasetError.value = ''
  try {
    const response = await fetch(apiBase + '/api/v1/datasets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: datasetName.value.trim() }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error('新建数据集失败，请检查名称或服务状态')
    datasets.value.push(payload)
    datasetReady.value = true
    datasetId.value = payload.id
    datasetName.value = ''
  } catch (cause) {
    datasetError.value = cause.message
  } finally {
    datasetCreating.value = false
  }
}

watch(datasetId, () => {
  datasetVersion += 1
  health.value = null
  items.value = []
  organizations.value = []
  notice.value = null
  batchResult.value = null
  selectedFiles.value = []
  selectedBuyerId.value = ''
  selectedSupplierId.value = ''
  selectedSupplierIds.value = []
  searchText.value = ''
  searchBrand.value = ''
  searchCategory.value = ''
  searched.value = false
  analyticsResults.value = {}
  analyticsLoading.value = ''
  analyticsError.value = ''
  error.value = ''
  try { localStorage.setItem('bidintel.dataset', datasetId.value) } catch { /* session only */ }
  if (datasetReady.value) Promise.all([refreshHealth(), searchItems(), refreshOrganizations()])
}, { flush: 'sync' })

onMounted(async () => {
  try {
    await refreshDatasets()
    let saved
    try { saved = localStorage.getItem('bidintel.dataset') } catch { /* default dataset */ }
    if (saved && datasets.value.some(row => row.id === saved)) datasetId.value = saved
    else if (saved && saved !== 'default') datasetError.value = '原先的数据集已不存在，当前显示默认数据集，请核对后再导入。'
    datasetReady.value = true
    await Promise.all([refreshHealth(), searchItems(), refreshOrganizations()])
  } catch (cause) {
    datasetError.value = cause.message
  }
  try {
    const response = await fetch(apiBase + '/api/v1/parser-capabilities')
    if (response.ok) capabilities.value = await response.json()
  } catch { /* report capability status as unknown */ }
  await loadModelConfig()
})
</script>

<template>
  <main class="shell">
    <header class="topbar">
      <div class="brandmark">采</div>
      <div class="brand-copy">
        <div class="eyebrow">ICT 创新大赛 · 赛题五</div>
        <h1>招采数据分析台</h1>
      </div>
      <div class="connection"><span class="dot" :class="{ offline: !health }"></span>{{ statusText }}</div>
    </header>

    <section class="intro">
      <div>
        <p class="eyebrow">PROCUREMENT INTELLIGENCE</p>
        <h2>从公告和附件里，<br /><span>找到可验证的业务线索。</span></h2>
        <p class="intro-copy">先把原始招采材料整理成可追溯的标的物记录。每条候选数据保留来源文件与表格位置，便于核验。</p>
      </div>
      <div class="intro-badge"><span>01</span><small>数据接入<br />与字段抽取</small></div>
    </section>

    <section class="panel config-panel">
      <div class="panel-heading">
        <div><span class="step">00</span><h3>模型配置</h3></div>
        <span class="hint">{{ modelConfig.configured ? `已启用 ${modelConfig.model_name}` : '未配置：仅用表格映射，不抽取投标主体' }}</span>
      </div>
      <div class="config-grid">
        <label><span>接口地址</span><input v-model="modelConfig.model_base_url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" /></label>
        <label><span>模型名</span><input v-model="modelConfig.model_name" placeholder="qwen-plus / deepseek-chat" /></label>
        <label><span>API Key</span><input v-model="modelKeyInput" type="password" :placeholder="apiKeyPlaceholder" /></label>
      </div>
      <div class="button-row">
        <button class="secondary" :disabled="modelTesting" @click="testModelConfig">{{ modelTesting ? '测试中…' : '测试连接' }}</button>
        <button class="primary" :disabled="modelSaving" @click="saveModelConfig">{{ modelSaving ? '保存中…' : '保存配置' }} <span>↗</span></button>
      </div>
      <p v-if="modelMessage" class="notice" :class="{ 'is-error': modelMessageIsError }">{{ modelMessage }}</p>
    </section>

    <section class="panel dataset-panel">
      <div class="panel-heading"><div><h3>当前数据集</h3></div><span class="hint">导入、检索和五类查询使用同一数据集</span></div>
      <div class="config-grid">
        <label><span>选择数据集</span><select v-model="datasetId" :disabled="uploading || datasetCreating || !datasetReady">
          <option v-for="row in datasets" :key="row.id" :value="row.id">{{ row.name }}</option>
        </select></label>
        <label><span>新数据集名称</span><input v-model="datasetName" maxlength="100" placeholder="如：官方数据第一轮" :disabled="uploading || datasetCreating" /></label>
      </div>
      <div class="button-row"><button class="secondary" :disabled="uploading || datasetCreating || !datasetName.trim()" @click="createDataset">{{ datasetCreating ? '正在创建…' : '新建空数据集并切换' }}</button></div>
      <p class="batch-note">正式数据导入前先新建空数据集。原有数据会保留，可随时切回查看。标注工作台使用独立的 JSON 文件。</p>
      <p v-if="uploading" class="batch-note">导入期间固定使用当前数据集，完成后可切换。</p>
      <p v-if="datasetError" class="error">{{ datasetError }}</p>
    </section>

    <section class="panel import-panel">
      <div class="panel-heading">
        <div><span class="step">01</span><h3>导入公告材料</h3></div>
        <span class="hint">HTML 公告，可同时选择该公告对应的 ZIP 附件</span>
      </div>
      <div class="warning-list" v-if="!capabilities || !capabilities.legacy_doc || !capabilities.legacy_xls || !capabilities.pdf_tables || !capabilities.pdf_render || !capabilities.image_ocr">
        <p v-if="!capabilities">解析能力尚未确认，请检查后端连接。</p>
        <template v-else>
          <p v-if="!capabilities.legacy_doc">旧版 DOC 暂不可用：请安装 LibreOffice 并设置 LIBREOFFICE_PATH。</p>
          <p v-if="!capabilities.legacy_xls || !capabilities.pdf_tables || !capabilities.pdf_render">附件依赖不完整：请重新安装后端依赖，检查 xlrd、pdfplumber、pypdfium2。</p>
          <p v-if="!capabilities.image_ocr">未检测到 Tesseract。扫描件及图片需要安装 OCR 引擎与语言包，并启用 OCR_ENABLED。</p>
        </template>
      </div>
      <label class="dropzone" :key="datasetId">
        <input type="file" multiple accept=".html,.htm,.zip,.doc,.docx,.xls,.xlsx,.pdf,.txt,.png,.jpg,.jpeg,.tif,.tiff,.bmp" @change="onFileChange" />
        <span class="upload-icon">↑</span>
        <strong>{{ selectedFiles.length ? `已选择 ${selectedFiles.length} 个文件` : '选择公告文件或拖入文件' }}</strong>
        <small>支持 HTML、ZIP、DOC/DOCX、XLS/XLSX、PDF、TXT、图片；本次选择按一条公告处理</small>
        <div v-if="selectedFiles.length" class="file-list">
          <span v-for="file in selectedFiles" :key="file.name">{{ file.name }}</span>
        </div>
      </label>
      <div class="form-footer">
        <p>表格列名映射可直接提取；扫描件与复杂 PDF 会标记为待处理。</p>
        <div class="button-row">
          <button class="secondary" :disabled="!selectedFiles.length || uploading || !datasetReady" @click="importBatch">批量导入 ZIP</button>
          <button class="primary" :disabled="!selectedFiles.length || uploading || !datasetReady" @click="importNotice">
            {{ uploading ? '正在解析…' : '导入单条公告' }} <span>↗</span>
          </button>
        </div>
      </div>
      <p v-if="error" class="error">{{ error }}</p>
    </section>

    <section v-if="notice" class="panel result-panel">
      <div class="panel-heading">
        <div><span class="step">02</span><h3>本次解析结果</h3></div>
        <span class="count-pill">{{ notice.items_found }} 条候选标的</span>
      </div>
      <div class="metadata-grid">
        <div><small>项目名称</small><strong>{{ notice.metadata.project_name || '未识别' }}</strong></div>
        <div><small>采购单位</small><strong>{{ notice.metadata.procurement_unit || '未识别' }}</strong></div>
        <div><small>项目编号</small><strong>{{ notice.metadata.project_number || '未识别' }}</strong></div>
        <div><small>公告金额</small><strong>{{ prettyAmount(notice.metadata.announced_total_award) }}</strong></div>
      </div>
      <div v-if="notice.warnings.length" class="warning-list">
        <p v-for="warning in notice.warnings" :key="warning">{{ warning }}</p>
      </div>
    </section>

    <section v-if="batchResult" class="panel result-panel">
      <div class="panel-heading">
        <div><span class="step">02</span><h3>批量导入结果</h3></div>
        <span class="count-pill">{{ batchResult.notices_imported }} / {{ batchResult.notices_found }} 条公告</span>
      </div>
      <div class="metadata-grid">
        <div><small>候选标的</small><strong>{{ batchResult.total_items_found }}</strong></div>
        <div><small>投标主体</small><strong>{{ batchResult.total_participants_found }}</strong></div>
        <div><small>处理时间</small><strong>{{ batchResult.elapsed_seconds }} 秒</strong></div>
        <div><small>未匹配附件</small><strong>{{ batchResult.orphan_files.length }}</strong></div>
      </div>
      <div v-if="batchResult.orphan_files.length || batchResult.errors.length || batchResult.warnings?.length" class="warning-list">
        <p v-for="warning in batchResult.warnings || []" :key="warning">{{ warning }}</p>
        <p v-for="file in batchResult.orphan_files" :key="file">附件未能按文件名匹配公告：{{ file }}</p>
        <p v-for="entry in batchResult.errors" :key="entry">{{ entry }}</p>
      </div>
      <details v-for="row in batchResult.notices" :key="row.notice_id" class="batch-note">
        <summary>{{ row.source_files[0] }} · {{ row.items_found }} 条标的 · {{ row.warnings.length }} 条提示</summary>
        <p v-for="warning in row.warnings" :key="warning">{{ warning }}</p>
      </details>
    </section>

    <section class="panel records-panel">
      <div class="panel-heading">
        <div><span class="step">03</span><h3>标的物检索</h3></div>
        <span class="hint">{{ searched ? `${items.length} 条记录` : '可按名称、品牌、品目筛选' }}</span>
      </div>
      <form class="searchbar" @submit.prevent="searchItems">
        <label><span>产品或服务</span><input v-model="searchText" placeholder="例如：自助终端" /></label>
        <label><span>品目</span><input v-model="searchCategory" placeholder="例如：信息化设备" /></label>
        <label><span>品牌</span><input v-model="searchBrand" placeholder="例如：长城医疗" /></label>
        <button class="secondary">检索 <span>→</span></button>
      </form>
      <div class="table-wrap">
        <table>
          <thead><tr><th>产品 / 服务</th><th>品目</th><th>品牌</th><th>规格型号</th><th>数量</th><th>单价</th><th>总价</th><th>来源</th></tr></thead>
          <tbody>
            <tr v-for="item in items" :key="item.id">
              <td class="primary-cell">{{ item.product_name || '—' }}</td>
              <td>{{ item.category || '—' }}</td><td>{{ item.brand || '—' }}</td><td>{{ item.model || '—' }}</td>
              <td>{{ item.quantity ?? '—' }} {{ item.quantity_unit || '' }}</td>
              <td>{{ prettyAmount(item.unit_price) }}</td><td>{{ prettyAmount(item.total_price) }}</td>
              <td><span class="source-tag">{{ item.source_file }} · {{ item.source_location }}</span></td>
            </tr>
            <tr v-if="!items.length"><td colspan="8" class="empty">{{ searched ? '暂无匹配记录，导入公告后候选数据会显示在这里。' : '正在读取记录…' }}</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel analytics-panel">
      <div class="panel-heading">
        <div><span class="step">04</span><h3>主体关系分析</h3></div>
        <span class="hint">查询频次按采购包计；金额只累加已确认的中标记录</span>
      </div>
      <div class="query-grid">
        <article class="query-card">
          <div class="query-number">01</div><h4>采购单位合作方</h4>
          <label>选择采购单位<select v-model="selectedBuyerId"><option value="">选择主体</option><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <button class="secondary" :disabled="!selectedBuyerId || analyticsLoading === 'awardees'" @click="runScene('awardees')">{{ analyticsLoading === 'awardees' ? '查询中…' : '查询中标供应商' }} <span>→</span></button>
          <div v-if="analyticsResults.awardees" class="query-result">
            <p v-if="!analyticsResults.awardees.awardees.length" class="empty-note">暂无已确认的中标关系</p>
            <p v-for="supplier in analyticsResults.awardees.awardees" :key="supplier.organization_id"><strong>{{ supplier.name }}</strong><span>{{ supplier.award_package_count }} 包 · ¥{{ prettyAmount(supplier.award_amount_total) }}</span><small>相关品牌：{{ supplier.product_brands.join('、') || '—' }}</small></p>
          </div>
        </article>

        <article class="query-card">
          <div class="query-number">02</div><h4>高频投标主体</h4>
          <label>选择采购单位<select v-model="selectedBuyerId"><option value="">选择主体</option><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <button class="secondary" :disabled="!selectedBuyerId || analyticsLoading === 'bidders'" @click="runScene('bidders')">{{ analyticsLoading === 'bidders' ? '查询中…' : '查询主体与组合' }} <span>→</span></button>
          <div v-if="analyticsResults.bidders" class="query-result">
            <p v-for="bidder in analyticsResults.bidders.top_bidders" :key="bidder.id"><strong>{{ bidder.canonical_name }}</strong><span>{{ bidder.package_count }} 包</span></p>
            <small v-if="analyticsResults.bidders.co_bidder_pairs.length">共同投标：{{ analyticsResults.bidders.co_bidder_pairs.map(pair => `${pair.name1} + ${pair.name2}（${pair.package_count}）`).join('；') }}</small>
            <p v-if="!analyticsResults.bidders.top_bidders.length" class="empty-note">暂无已确认的投标关系</p>
          </div>
        </article>

        <article class="query-card">
          <div class="query-number">03</div><h4>中标供应商共同竞标方</h4>
          <label>选择中标供应商<select v-model="selectedSupplierId"><option value="">选择主体</option><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <button class="secondary" :disabled="!selectedSupplierId || analyticsLoading === 'coBidders'" @click="runScene('coBidders')">{{ analyticsLoading === 'coBidders' ? '查询中…' : '查询共同竞标方' }} <span>→</span></button>
          <div v-if="analyticsResults.coBidders" class="query-result">
            <p v-for="bidder in analyticsResults.coBidders.top_co_bidders" :key="bidder.organization_id"><strong>{{ bidder.canonical_name }}</strong><span>{{ bidder.package_count }} 包</span></p>
            <p v-if="!analyticsResults.coBidders.top_co_bidders.length" class="empty-note">暂无已确认的共同投标关系</p>
          </div>
        </article>

        <article class="query-card wide-card">
          <div class="query-number">04–05</div><h4>多主体共同合作 / 共同投标</h4>
          <label>选择至少两家主体（按住 Ctrl 可多选）<select v-model="selectedSupplierIds" multiple><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <div class="button-row">
            <button class="secondary" :disabled="selectedSupplierIds.length < 2 || analyticsLoading === 'commonBuyers'" @click="runScene('commonBuyers')">共同合作采购单位 <span>→</span></button>
            <button class="secondary" :disabled="selectedSupplierIds.length < 2 || analyticsLoading === 'commonProjects'" @click="runScene('commonProjects')">共同投标项目 <span>→</span></button>
          </div>
          <div class="split-results">
            <div v-if="analyticsResults.commonBuyers" class="query-result">
              <strong class="result-label">共同合作采购单位</strong>
              <p v-for="entry in analyticsResults.commonBuyers.buyers" :key="entry.buyer.id"><strong>{{ entry.buyer.canonical_name }}</strong><span>中标金额合计 ¥{{ prettyAmount(entry.award_amount_total_unique_awards) }}</span><small>{{ entry.suppliers.map(s => `${s.name}：${s.award_package_count} 包`).join('；') }}</small></p>
              <p v-if="!analyticsResults.commonBuyers.buyers.length" class="empty-note">暂无共同合作采购单位</p>
            </div>
            <div v-if="analyticsResults.commonProjects" class="query-result">
              <strong class="result-label">共同投标项目 · {{ analyticsResults.commonProjects.project_count }} 个项目 / {{ analyticsResults.commonProjects.package_count }} 个采购包</strong>
              <p v-for="entry in analyticsResults.commonProjects.packages" :key="entry.package_id"><strong>{{ entry.project_name || entry.project_number }}</strong><span>成交金额 ¥{{ prettyAmount(entry.award_amount_total_unique_awards) }}</span><small>{{ entry.participants.map(row => `${row.canonical_name}（${row.outcome}）`).join('、') }}</small></p>
              <p v-if="!analyticsResults.commonProjects.packages.length" class="empty-note">暂无共同投标项目</p>
            </div>
          </div>
        </article>
      </div>
      <p v-if="analyticsError" class="error">{{ analyticsError }}</p>
    </section>

    <AnnotationWorkbench :api-base="apiBase" />
    <RelationshipGraph v-if="datasetReady" :api-base="apiBase" :dataset-id="datasetId" :refresh-key="health?.notices_imported || 0" />
    <footer>数据抽取为候选结果，进入竞赛验证集前应进行人工抽样核验。<span>数据留痕 · 结果可核验 · 关系可追溯</span></footer>
  </main>
</template>
