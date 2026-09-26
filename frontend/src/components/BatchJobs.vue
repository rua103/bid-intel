<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { createDatasetClient } from '../utils/datasets.js'

const props = defineProps({ apiBase: String, datasetId: String })
const emit = defineEmits(['updated'])
const source = ref('')
const mode = ref('rules')
const ocr = ref(true)
const jobs = ref([])
const error = ref('')
const busy = ref(false)
let version = 0
let timer
let disposed = false
let lastDone = -1
const client = createDatasetClient(() => props.datasetId, () => version)
const labels = { queued: '等待启动', running: '处理中', paused: '已暂停', interrupted: '已中断，可续跑', completed: '已完成', completed_with_errors: '完成，有失败公告' }

async function request(path, options) {
  const response = await client(props.apiBase + '/api/v1/jobs' + path, options)
  const result = await response.json()
  if (!response.ok) throw new Error(result.detail || '任务请求失败')
  return result
}
async function refresh() {
  clearTimeout(timer)
  const current = version
  try {
    jobs.value = await request('')
    const done = jobs.value.reduce((sum, job) => sum + job.done, 0)
    if (done !== lastDone) { lastDone = done; emit('updated') }
  } catch (cause) {
    if (cause.name !== 'AbortError') error.value = cause.message
  } finally {
    if (!disposed && current === version) timer = setTimeout(refresh, 3000)
  }
}
async function create() {
  busy.value = true
  error.value = ''
  try {
    await request('', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_directory: source.value, mode: mode.value, ocr: ocr.value }) })
    await refresh()
  } catch (cause) { if (cause.name !== 'AbortError') error.value = cause.message }
  finally { busy.value = false }
}
async function action(job, operation) {
  try { await request('/' + job.id + '/' + operation, { method: 'POST' }); await refresh() }
  catch (cause) { if (cause.name !== 'AbortError') error.value = cause.message }
}
async function report(job) {
  try {
    const payload = await request('/' + job.id + '/report')
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url; link.download = 'batch-' + job.id + '.json'; link.click()
    URL.revokeObjectURL(url)
  } catch (cause) { if (cause.name !== 'AbortError') error.value = cause.message }
}
watch(() => props.datasetId, () => { version++; jobs.value = []; error.value = ''; lastDone = -1; refresh() }, { immediate: true })
onBeforeUnmount(() => { disposed = true; version++; clearTimeout(timer) })
</script>

<template>
  <section class="panel batch-jobs">
    <div class="panel-heading"><h3>大批量后台处理</h3><span class="hint">关闭页面后继续运行，进度保存在本机</span></div>
    <p class="batch-note">先把 HTML 公告和同名 ZIP / RAR / 7z 放在后端所在电脑的同一目录。结果写入当前数据集；重跑整套数据前建议新建数据集。</p>
    <form class="job-form" @submit.prevent="create">
      <label>材料目录<input v-model="source" required placeholder="例如 D:/path/to/official-data" /></label>
      <label>抽取方式<select v-model="mode"><option value="rules">规则抽取</option><option value="hybrid">规则＋已配置模型</option><option value="model">已配置模型</option></select></label>
      <label class="ocr-option"><input v-model="ocr" type="checkbox" />本地中文 OCR</label>
      <button class="primary" :disabled="busy">{{ busy ? '创建任务…' : '开始后台处理' }}</button>
    </form>
    <p v-if="mode !== 'rules'" class="batch-note">模型模式使用已配置的服务，会产生 API 调用费用。</p>
    <p v-if="error" class="error">{{ error }}</p>
    <article v-for="job in jobs" :key="job.id" class="job-row">
      <div class="job-title"><strong>{{ labels[job.status] || job.status }}</strong><span>{{ job.done }} / {{ job.notices_total }} 条 · {{ job.items }} 条候选 · {{ job.failed }} 条失败</span></div>
      <progress :value="job.done + job.failed" :max="job.notices_total" />
      <p class="batch-note">{{ job.source }} · {{ job.mode }} · {{ job.ocr ? 'OCR 开启' : 'OCR 关闭' }}</p>
      <p v-for="entry in job.active" :key="entry.index" class="active-file">{{ entry.name }} · {{ entry.documents_done }} / {{ entry.documents_total || '…' }} 份材料<br />{{ entry.current_file }}</p>
      <div class="job-actions">
        <button v-if="job.status === 'running'" class="secondary" @click="action(job, 'pause')">处理完当前文件后暂停</button>
        <button v-else-if="!['completed', 'completed_with_errors'].includes(job.status)" class="secondary" @click="action(job, 'resume')">继续处理</button>
        <button v-if="job.failed && job.status !== 'running'" class="secondary" @click="action(job, 'resume?retry_failed=true')">重试失败公告</button>
        <button class="secondary" @click="report(job)">下载处理报告</button>
      </div>
      <details v-if="job.errors.length"><summary>失败原因</summary><p v-for="entry in job.errors" :key="entry.name">{{ entry.name }}：{{ entry.error }}</p></details>
    </article>
  </section>
</template>

<style scoped>
.job-form { display:flex; flex-wrap:wrap; gap:16px; align-items:end; }
.job-form label { display:flex; flex-direction:column; gap:7px; flex:1; min-width:170px; }
.job-form .ocr-option { flex-direction:row; align-items:center; flex:0 0 auto; min-width:auto; padding-bottom:12px; }
.ocr-option input { width:auto; }
.job-row { border-top:1px solid #dce4e8; margin-top:24px; padding-top:20px; }
.job-title, .job-actions { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; }
.job-actions { justify-content:flex-start; }
progress { width:100%; height:12px; margin-top:14px; accent-color:#187d73; }
.active-file { font-size:12px; overflow-wrap:anywhere; color:#59716e; }
</style>
