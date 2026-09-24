<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  apiBase: { type: String, required: true },
  limit: { type: Number, default: 100 },
  refreshKey: { type: [String, Number], default: 0 },
})

const chartElement = ref(null)
const loading = ref(false)
const error = ref('')
const graph = ref(null)
let chart = null

const colors = ['#166534', '#155e75', '#9a3412', '#6b21a8']

function render() {
  if (!chart || !graph.value) return
  const payload = graph.value
  const categories = (payload.categories || []).map((name) => ({ name }))
  const categoryIndex = Object.fromEntries(categories.map((item, index) => [item.name, index]))
  chart.setOption({
    animationDuration: 350,
    tooltip: { formatter: (params) => params.dataType === 'edge' ? params.data.relation : params.data.name },
    legend: [{ data: categories.map((item) => item.name), top: 8, left: 'center' }],
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      data: (payload.nodes || []).map((node) => ({
        id: node.id, name: node.name, category: categoryIndex[node.category] ?? 0,
        value: node.properties, symbolSize: node.category === 'project' ? 34 : 22,
      })),
      links: (payload.links || []).map((link) => ({
        source: link.source, target: link.target, relation: link.relation,
        lineStyle: { opacity: 0.55, curveness: 0.08 },
      })),
      categories,
      label: { show: true, fontSize: 11, formatter: '{b}' },
      edgeLabel: { show: false },
      lineStyle: { color: 'source' },
      itemStyle: { borderColor: '#fff', borderWidth: 1 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
      force: { repulsion: 180, edgeLength: [80, 180], gravity: 0.08 },
    }],
    color: colors,
  })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await fetch(`${props.apiBase}/api/v1/graph?limit=${Math.max(1, props.limit)}`)
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || '关系图读取失败')
    graph.value = result
    await nextTick()
    render()
  } catch (cause) {
    error.value = cause.message || '关系图读取失败'
  } finally {
    loading.value = false
  }
}

function resize() { chart?.resize() }

onMounted(() => {
  chart = echarts.init(chartElement.value)
  window.addEventListener('resize', resize)
  load()
})
watch(() => props.refreshKey, load)
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
})
</script>

<template>
  <section class="relationship-graph" aria-label="主体关系图">
    <div class="graph-heading">
      <div>
        <strong>主体关系图</strong>
        <small v-if="graph">SQLite 图谱 · {{ graph.project_count }} / {{ graph.total_project_count }} 个项目</small>
      </div>
      <button type="button" class="secondary" :disabled="loading" @click="load">{{ loading ? '读取中…' : '刷新' }}</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div ref="chartElement" class="graph-canvas"></div>
    <p class="graph-note">当前展示来自 SQLite 的可视化投影；Neo4j 同步是可选后端，不影响此图。</p>
  </section>
</template>

<style scoped>
.relationship-graph { margin-top: 1.25rem; border: 1px solid #d9e2dc; border-radius: 18px; background: #fff; overflow: hidden; }
.graph-heading { display: flex; justify-content: space-between; align-items: center; padding: 1rem 1.2rem; border-bottom: 1px solid #edf1ee; }
.graph-heading strong, .graph-heading small { display: block; }
.graph-heading small { margin-top: .25rem; color: #708077; font-size: .78rem; }
.graph-canvas { width: 100%; height: 430px; }
.graph-note { margin: 0; padding: .8rem 1.2rem 1rem; color: #708077; font-size: .78rem; }
</style>
