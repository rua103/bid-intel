<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import RelationshipGraph from './components/RelationshipGraph.vue'
import BatchJobs from './components/BatchJobs.vue'
import AuthGate from './components/AuthGate.vue'
import NoticeEvidenceDialog from './components/NoticeEvidenceDialog.vue'
import RelationshipClues from './components/RelationshipClues.vue'
import ControlledQuery from './components/ControlledQuery.vue'
import { resolveApiBase } from './utils/browser.js'
import { createDatasetClient } from './utils/datasets.js'
import { formatAmount } from './utils/amountPolicy.js'
import { outcomeLabel, participationScopeLabel, productSupplierEvidence, productSupplierMetrics } from './utils/analyticsPresentation.js'
import { navigationGroups, resolveSection, sectionHash, sectionTransition } from './utils/navigation.js'

const apiBase = resolveApiBase(import.meta.env.VITE_API_BASE, window.location, import.meta.env.VITE_API_PORT || '8000')
const activeSection = ref(resolveSection(window.location.hash))
const selectedFiles = ref([])
const uploading = ref(false)
const notice = ref(null)
const batchResult = ref(null)
const error = ref('')
const health = ref(null)
const connectionError = ref('')
const datasets = ref([])
const datasetId = ref('default')
const datasetName = ref('')
const datasetError = ref('')
const datasetReady = ref(false)
const datasetCreating = ref(false)
const capabilities = ref(null)
let datasetVersion = 0
const apiFetch = (url, options = {}) => fetch(url, { ...options, credentials: 'include' })
const datasetFetch = createDatasetClient(() => datasetId.value, () => datasetVersion, apiFetch)
const searchText = ref('')
const searchBrand = ref('')
const searchCategory = ref('')
const items = ref([])
const searched = ref(false)
const noticeDetail = ref(null)
const noticeDetailItemId = ref(null)
const noticeDetailLoading = ref(false)
const noticeDetailError = ref('')
const organizations = ref([])
const selectedBuyerId = ref('')
const selectedSupplierId = ref('')
const selectedSupplierIds = ref([])
const analyticsResults = ref({})
const analyticsLoading = ref('')
const analyticsError = ref('')
const analyticsBackend = ref('')
const analyticsFallback = ref(false)
const modelConfig = ref({ model_base_url: '', model_name: '', model_api_key_masked: '', api_key_configured: false, configured: false })
const modelKeyInput = ref('')
const modelSaving = ref(false)
const modelTesting = ref(false)
const modelMessage = ref('')
const modelMessageIsError = ref(false)
const apiKeyPlaceholder = computed(() => modelConfig.value.api_key_configured ? `已配置（${modelConfig.value.model_api_key_masked}）留空保持不变` : 'sk-...')
const prettyAmount = (value) => formatAmount(value)
const productSupplierRows = (supplier) => {
  const rows = supplier?.product_suppliers ?? supplier?.product_supplier_summary ?? supplier?.productSuppliers
  if (Array.isArray(rows) && rows.length) return rows
  const brands = supplier?.product_brands ?? supplier?.brands ?? []
  return (Array.isArray(brands) ? brands : []).map((name) => ({ name, legacy: true }))
}
const productSuppliersOf = (payload) => Array.isArray(payload?.product_suppliers) ? payload.product_suppliers : []
const productSupplierName = (row) => typeof row === 'string'
  ? row
  : row?.name ?? row?.canonical_name ?? row?.supplier_name ?? row?.brand ?? row?.value ?? '未命名产品供应商'
const participationLabel = (payload) => `参与口径：${participationScopeLabel(payload)}`
const activeSectionLabel = computed(() => navigationGroups
  .flatMap((group) => group.items)
  .find((item) => item.id === activeSection.value)?.label || '首页概览')
const pageTransition = computed(() => `page-${sectionTransition(activeSection.value)}`)
const currentDatasetName = computed(() => datasets.value.find((row) => row.id === datasetId.value)?.name || '正在读取')

const statusText = computed(() => {
  if (!health.value) return '等待连接后端'
  return `已导入 ${health.value.notices_imported} 条公告`
})

function navigate(section) {
  activeSection.value = resolveSection(section)
  window.history.replaceState(null, '', sectionHash(activeSection.value))
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function syncSectionFromHash() {
  activeSection.value = resolveSection(window.location.hash)
}

async function refreshHealth() {
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/health`)
    if (!response.ok) throw new Error('后端暂不可用')
    health.value = await response.json()
    connectionError.value = ''
  } catch (cause) {
    if (cause.name === 'AbortError') return
    health.value = null
    connectionError.value = cause instanceof TypeError
      ? '无法连接后端 API。请检查演示主机、网络、防火墙端口和 CORS 配置。'
      : cause.message || '后端暂不可用，请检查服务状态。'
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

async function openNoticeDetail(noticeId, itemId = null) {
  noticeDetail.value = null
  noticeDetailItemId.value = itemId
  noticeDetailError.value = ''
  noticeDetailLoading.value = true
  try {
    const response = await datasetFetch(`${apiBase}/api/v1/notices/${noticeId}`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '公告详情读取失败')
    noticeDetail.value = payload
  } catch (cause) {
    if (cause.name === 'AbortError') return
    noticeDetailError.value = cause.message || '公告详情读取失败'
  } finally {
    noticeDetailLoading.value = false
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
    const response = await apiFetch(`${apiBase}/api/v1/model-config`)
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
    const response = await apiFetch(`${apiBase}/api/v1/model-config`, {
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
    const response = await apiFetch(`${apiBase}/api/v1/model-config/test`, {
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
    analyticsBackend.value = response.headers.get('X-Analytics-Backend') || 'sqlite'
    analyticsFallback.value = response.headers.get('X-Analytics-Fallback') === 'sqlite'
    analyticsResults.value = { ...analyticsResults.value, [scene]: payload }
  } catch (cause) {
    if (cause.name === 'AbortError') return
    analyticsError.value = cause.message || '关系查询失败'
  } finally {
    if (version === datasetVersion) analyticsLoading.value = ''
  }
}

async function refreshDatasets() {
  const response = await apiFetch(apiBase + '/api/v1/datasets')
  if (!response.ok) throw new Error('数据集列表读取失败')
  datasets.value = await response.json()
}

async function createDataset() {
  if (!datasetName.value.trim() || uploading.value) return
  datasetCreating.value = true
  datasetError.value = ''
  try {
    const response = await apiFetch(apiBase + '/api/v1/datasets', {
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
  noticeDetail.value = null
  noticeDetailItemId.value = null
  noticeDetailError.value = ''
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
  analyticsBackend.value = ''
  analyticsFallback.value = false
  error.value = ''
  try { localStorage.setItem('bidintel.dataset', datasetId.value) } catch { /* session only */ }
  if (datasetReady.value) Promise.all([refreshHealth(), searchItems(), refreshOrganizations()])
}, { flush: 'sync' })

onMounted(async () => {
  window.addEventListener('hashchange', syncSectionFromHash)
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
    const response = await apiFetch(apiBase + '/api/v1/parser-capabilities')
    if (response.ok) capabilities.value = await response.json()
  } catch { /* report capability status as unknown */ }
  await loadModelConfig()
})

onBeforeUnmount(() => window.removeEventListener('hashchange', syncSectionFromHash))
</script>

<template>
  <AuthGate :api-base="apiBase">
  <div class="dashboard-shell">
    <aside class="dashboard-sidebar">
      <div class="sidebar-brand">
        <span class="sidebar-brandmark">采</span>
        <span><strong>招采数据分析台</strong><small>Bid Intelligence</small></span>
      </div>
      <nav class="sidebar-navigation" aria-label="主导航">
        <div v-for="group in navigationGroups" :key="group.label" class="nav-group">
          <p>{{ group.label }}</p>
          <button v-for="item in group.items" :key="item.id" type="button" class="nav-item"
            :class="{ active: activeSection === item.id }" :aria-current="activeSection === item.id ? 'page' : undefined"
            @click="navigate(item.id)">
            <span class="nav-marker"></span>{{ item.label }}<span class="nav-arrow">›</span>
          </button>
        </div>
      </nav>
      <div class="sidebar-foot"><span class="dot" :class="{ offline: !health }"></span>{{ health ? '后端服务正常' : '后端暂未连接' }}</div>
    </aside>

    <main class="dashboard-main">
      <header class="workspace-topbar">
        <div class="workspace-title"><span>招采数据分析台</span><strong>{{ activeSectionLabel }}</strong></div>
        <div class="workspace-status">
          <button type="button" @click="navigate('datasets')"><small>当前数据集</small><strong>{{ currentDatasetName }}</strong></button>
          <span><small>公告数量</small><strong>{{ health?.notices_imported ?? '—' }}</strong></span>
          <button type="button" @click="navigate('batch')"><small>后台任务</small><strong>查看状态</strong></button>
        </div>
      </header>

      <div class="workspace-content">
        <p v-if="connectionError" class="notice is-error connection-banner" role="alert">
          {{ connectionError }} <button type="button" class="secondary" @click="refreshHealth">重试连接</button>
        </p>
        <Transition :name="pageTransition" mode="out-in">
        <div :key="activeSection" class="page-stage">
        <section v-if="activeSection === 'overview'" class="overview-view">
          <div class="overview-hero">
            <div>
              <p class="eyebrow">PROCUREMENT INTELLIGENCE</p>
              <h1>从公告和附件中，<br /><span>找到可验证的业务线索。</span></h1>
              <p>把原始招采材料整理成可追溯的标的物记录，并分析采购单位、供应商和项目之间的关系。</p>
            </div>
            <div class="overview-summary">
              <small>当前工作空间</small>
              <strong>{{ currentDatasetName }}</strong>
              <span>{{ statusText }}</span>
            </div>
          </div>
          <div class="overview-actions">
            <button type="button" @click="navigate('import')"><span>01</span><strong>导入公告材料</strong><small>上传单条公告及相关附件</small></button>
            <button type="button" @click="navigate('batch')"><span>02</span><strong>大批量后台处理</strong><small>处理官方数据目录并查看进度</small></button>
            <button type="button" @click="navigate('items')"><span>03</span><strong>检索标的物</strong><small>按名称、品目和品牌筛选</small></button>
            <button type="button" @click="navigate('analytics')"><span>04</span><strong>分析主体关系</strong><small>查看投标、中标与合作关系</small></button>
          </div>
          <div class="overview-note">
            <div><strong>建议工作顺序</strong><p>建立数据集 → 导入材料 → 核验标的物 → 分析主体关系 → 进入质量评测。</p></div>
            <button type="button" class="secondary" @click="navigate('quality')">进入质量评测 <span>→</span></button>
          </div>
        </section>

    <section v-if="activeSection === 'model'" class="panel config-panel view-panel">
      <div class="panel-heading">
        <div><span class="step">00</span><h3>模型配置</h3></div>
        <span class="hint">{{ modelConfig.configured ? `已启用 ${modelConfig.model_name}` : '未配置：仅用表格映射，不抽取投标主体' }}</span>
      </div>
      <div class="config-grid">
        <label><span>接口地址</span><input v-model="modelConfig.model_base_url" placeholder="https://api.deepseek.com" /></label>
        <label><span>模型名</span><input v-model="modelConfig.model_name" placeholder="deepseek-flash / qwen-plus" /></label>
        <label><span>API Key</span><input v-model="modelKeyInput" type="password" :placeholder="apiKeyPlaceholder" /></label>
      </div>
      <div class="button-row">
        <button class="secondary" :disabled="modelTesting" @click="testModelConfig">{{ modelTesting ? '测试中…' : '测试连接' }}</button>
        <button class="primary" :disabled="modelSaving" @click="saveModelConfig">{{ modelSaving ? '保存中…' : '保存配置' }} <span>↗</span></button>
      </div>
      <p v-if="modelMessage" class="notice" :class="{ 'is-error': modelMessageIsError }">{{ modelMessage }}</p>
    </section>

    <section v-if="activeSection === 'datasets'" class="panel dataset-panel view-panel">
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

    <section v-if="activeSection === 'import'" class="panel import-panel view-panel">
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

    <BatchJobs v-if="datasetReady && activeSection === 'batch'" :api-base="apiBase" :dataset-id="datasetId" @updated="refreshHealth" />

    <section v-if="notice && activeSection === 'import'" class="panel result-panel">
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
      <div class="button-row evidence-entry">
        <button type="button" class="secondary" @click="openNoticeDetail(notice.notice_id)">查看公告与来源证据 <span>→</span></button>
      </div>
    </section>

    <section v-if="batchResult && activeSection === 'import'" class="panel result-panel">
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
        <button type="button" class="source-open" @click="openNoticeDetail(row.notice_id)">查看公告与来源证据</button>
        <p v-for="warning in row.warnings" :key="warning">{{ warning }}</p>
      </details>
    </section>

    <section v-if="activeSection === 'items'" class="panel records-panel view-panel">
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
              <td>
                <span class="source-tag">{{ item.source_file }} · {{ item.source_location }}</span>
                <button type="button" class="source-open" @click="openNoticeDetail(item.notice_id, item.id)">公告证据</button>
              </td>
            </tr>
            <tr v-if="!items.length"><td colspan="8" class="empty">{{ searched ? '暂无匹配记录，导入公告后候选数据会显示在这里。' : '正在读取记录…' }}</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <NoticeEvidenceDialog
      :notice="noticeDetail"
      :initial-item-id="noticeDetailItemId"
      :loading="noticeDetailLoading"
      :error="noticeDetailError"
      @close="noticeDetail = null; noticeDetailItemId = null; noticeDetailError = ''"
    />

    <RelationshipClues
      v-if="datasetReady && activeSection === 'analytics'"
      :api-base="apiBase"
      :dataset-id="datasetId"
      :refresh-key="health?.notices_imported || 0"
      :fetch-impl="apiFetch"
      @open-notice="openNoticeDetail($event.noticeId, $event.itemId || null)"
    />

    <section v-if="activeSection === 'analytics'" class="panel analytics-panel view-panel">
      <ControlledQuery :api-base="apiBase" :dataset-id="datasetId" :fetch-impl="apiFetch" />
      <div class="panel-heading">
        <div><span class="step">04</span><h3>主体关系分析</h3></div>
        <span class="hint">查询频次按项目计；中标金额与标的总价分别统计</span>
      </div>
      <p v-if="analyticsBackend" class="analytics-backend-note">
        当前分析查询使用 {{ analyticsBackend === 'neo4j' ? 'Neo4j' : 'SQLite' }}{{ analyticsFallback ? '（Neo4j 不可用，已回退）' : '' }}
      </p>
      <div class="query-grid">
        <article class="query-card">
          <div class="query-number">01</div><h4>采购单位合作方</h4>
          <label>选择采购单位<select v-model="selectedBuyerId"><option value="">选择主体</option><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <button class="secondary" :disabled="!selectedBuyerId || analyticsLoading === 'awardees'" @click="runScene('awardees')">{{ analyticsLoading === 'awardees' ? '查询中…' : '查询中标供应商' }} <span>→</span></button>
          <div v-if="analyticsResults.awardees" class="query-result">
            <p v-if="!analyticsResults.awardees.awardees.length" class="empty-note">暂无已确认的中标关系</p>
            <p v-for="supplier in analyticsResults.awardees.awardees" :key="supplier.organization_id">
              <strong>{{ supplier.name }}</strong><span>{{ supplier.award_project_count }} 个合作项目 · {{ supplier.award_package_count }} 个采购包 · ¥{{ prettyAmount(supplier.award_amount_total) }}</span>
              <small>中标供应商；标的品牌：{{ productSupplierRows(supplier).map(productSupplierName).join('、') || '—' }}</small>
              <small v-for="(productSupplier, productSupplierIndex) in productSupplierRows(supplier)" :key="`${supplier.organization_id}-product-${productSupplierIndex}`" class="product-supplier-detail">
                {{ productSupplierName(productSupplier) }}<template v-if="productSupplierMetrics(productSupplier)">：{{ productSupplierMetrics(productSupplier) }}</template><template v-if="productSupplierEvidence(productSupplier)"> · {{ productSupplierEvidence(productSupplier) }}</template>
              </small>
            </p>
            <small v-if="productSuppliersOf(analyticsResults.awardees).length" class="query-semantics">产品供应商汇总按标的品牌分组；品牌不等同于法律实体，金额仅累加已披露的标的总价。</small>
            <p v-for="(productSupplier, productSupplierIndex) in productSuppliersOf(analyticsResults.awardees)" :key="`product-supplier-${productSupplierIndex}-${productSupplierName(productSupplier)}`">
              <strong>标的品牌：{{ productSupplierName(productSupplier) }}</strong>
              <span>{{ productSupplierMetrics(productSupplier) || '暂无汇总' }}</span>
              <small>来源状态：{{ productSupplierEvidence(productSupplier) }}</small>
            </p>
          </div>
        </article>

        <article class="query-card">
          <div class="query-number">02</div><h4>高频投标主体</h4>
          <label>选择采购单位<select v-model="selectedBuyerId"><option value="">选择主体</option><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <button class="secondary" :disabled="!selectedBuyerId || analyticsLoading === 'bidders'" @click="runScene('bidders')">{{ analyticsLoading === 'bidders' ? '查询中…' : '查询主体与组合' }} <span>→</span></button>
          <div v-if="analyticsResults.bidders" class="query-result">
            <small class="query-semantics">{{ participationLabel(analyticsResults.bidders) }}</small>
            <p v-for="bidder in analyticsResults.bidders.top_bidders" :key="bidder.id"><strong>{{ bidder.canonical_name }}</strong><span>{{ bidder.project_count }} 个项目</span></p>
            <small v-if="analyticsResults.bidders.co_bidder_pairs.length">共同投标项目：{{ analyticsResults.bidders.co_bidder_pairs.map(pair => `${pair.name1} + ${pair.name2}（${pair.project_count}）`).join('；') }}</small>
            <p v-if="!analyticsResults.bidders.top_bidders.length" class="empty-note">暂无已确认的投标关系</p>
          </div>
        </article>

        <article class="query-card">
          <div class="query-number">03</div><h4>中标供应商共同竞标方</h4>
          <label>选择中标供应商<select v-model="selectedSupplierId"><option value="">选择主体</option><option v-for="org in organizations" :key="org.id" :value="String(org.id)">{{ org.canonical_name }}</option></select></label>
          <button class="secondary" :disabled="!selectedSupplierId || analyticsLoading === 'coBidders'" @click="runScene('coBidders')">{{ analyticsLoading === 'coBidders' ? '查询中…' : '查询共同竞标方' }} <span>→</span></button>
          <div v-if="analyticsResults.coBidders" class="query-result">
            <small class="query-semantics">{{ participationLabel(analyticsResults.coBidders) }}</small>
            <p v-for="bidder in analyticsResults.coBidders.top_co_bidders" :key="bidder.organization_id"><strong>{{ bidder.canonical_name }}</strong><span>{{ bidder.project_count }} 个项目 · {{ bidder.award_package_count }} 个采购包</span><small>共同参与包中的所选供应商中标金额 ¥{{ prettyAmount(bidder.selected_supplier_award_amount_total) }}</small></p>
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
              <small class="query-semantics">各供应商分别统计与该采购单位的合作项目数；不要求在同一项目或同一采购包中标。</small>
              <p v-for="entry in analyticsResults.commonBuyers.buyers" :key="entry.buyer.id"><strong>{{ entry.buyer.canonical_name }}</strong><span>中标金额合计 ¥{{ prettyAmount(entry.award_amount_total_unique_awards) }}</span><small>{{ entry.suppliers.map(s => `${s.name}：${s.award_project_count} 个合作项目 · ${s.award_package_count} 个采购包`).join('；') }}</small></p>
              <p v-if="!analyticsResults.commonBuyers.buyers.length" class="empty-note">暂无共同合作采购单位</p>
            </div>
            <div v-if="analyticsResults.commonProjects" class="query-result">
              <strong class="result-label">共同投标项目 · {{ analyticsResults.commonProjects.project_count }} 个项目 / {{ analyticsResults.commonProjects.package_count }} 个采购包</strong>
              <p><strong>共同项目中标金额合计</strong><span>¥{{ prettyAmount(analyticsResults.commonProjects.award_amount_total_unique_awards) }}</span></p>
              <p v-for="project in analyticsResults.commonProjects.projects" :key="project.project_id"><strong>{{ project.project_name || project.project_number || `项目 ${project.project_id}` }}</strong><span>{{ project.package_count }} 个采购包 · 项目中标金额合计 ¥{{ prettyAmount(project.award_amount_total_unique_awards) }}</span></p>
              <p v-for="entry in analyticsResults.commonProjects.packages" :key="entry.package_id"><strong>{{ entry.project_name || entry.project_number }}</strong><span>成交金额 ¥{{ prettyAmount(entry.award_amount_total_unique_awards) }}</span><small>{{ (entry.participants || []).map(row => `${row.canonical_name}（${outcomeLabel(row.outcome)}）`).join('、') || '未返回参与主体' }}</small></p>
              <p v-if="!analyticsResults.commonProjects.packages.length" class="empty-note">暂无共同投标项目</p>
            </div>
          </div>
        </article>
      </div>
      <p v-if="analyticsError" class="error">{{ analyticsError }}</p>
    </section>

        <RelationshipGraph v-if="datasetReady && activeSection === 'analytics'" :api-base="apiBase" :dataset-id="datasetId" :refresh-key="health?.notices_imported || 0" />

        <section v-if="activeSection === 'quality'" class="quality-entry">
          <div class="quality-copy">
            <p class="eyebrow">QUALITY REVIEW</p>
            <h2>人工标注与质量评测</h2>
            <p>正式分析台只保留评测入口。标注、逐字段审核和报告导出继续在独立工作台中完成，避免长表单挤占日常分析空间。</p>
            <a class="primary quality-link" href="/annotation.html">打开人工标注台 <span>↗</span></a>
          </div>
          <div class="quality-steps">
            <article><span>01</span><div><strong>导入待标定数据</strong><p>载入公告原文以及待核验的实体和关系。</p></div></article>
            <article><span>02</span><div><strong>逐字段审核</strong><p>修正漏提、错提与证据位置，形成 gold 数据。</p></div></article>
            <article><span>03</span><div><strong>运行质量评测</strong><p>对照 predictions 查看指标和逐字段错误。</p></div></article>
            <article><span>04</span><div><strong>导出审核记录</strong><p>保存本轮修订和评测结果，便于后续复盘。</p></div></article>
          </div>
          <div class="test-only-note"><strong>测试阶段功能</strong><span>人工标注与审核工具用于完善比赛数据和检查抽取质量，正式交付版本可以移除。</span></div>
        </section>

        </div>
        </Transition>
        <footer>数据抽取为候选结果，进入竞赛验证集前应进行人工抽样核验。<span>数据留痕 · 结果可核验 · 关系可追溯</span></footer>
      </div>
    </main>
  </div>
  </AuthGate>
</template>
