import assert from 'node:assert/strict'
import test from 'node:test'

async function loadNavigation() {
  try {
    return await import('../src/utils/navigation.js')
  } catch (cause) {
    assert.fail(`navigation module is required: ${cause.message}`)
  }
}

test('dashboard navigation exposes every approved workspace section', async () => {
  const { navigationGroups } = await loadNavigation()
  assert.deepEqual(navigationGroups, [
    { label: '工作台', items: [{ id: 'overview', label: '首页概览' }] },
    { label: '数据接入', items: [{ id: 'import', label: '单条导入' }, { id: 'batch', label: '批量处理' }] },
    { label: '分析应用', items: [{ id: 'items', label: '标的物检索' }, { id: 'analytics', label: '主体关系分析' }, { id: 'quality', label: '质量评测' }] },
    { label: '系统设置', items: [{ id: 'datasets', label: '数据集管理' }, { id: 'model', label: '模型配置' }] },
  ])
})

test('dashboard section resolver accepts known hashes and rejects unknown ones', async () => {
  const { resolveSection, sectionHash } = await loadNavigation()
  assert.equal(resolveSection('#items'), 'items')
  assert.equal(resolveSection('analytics'), 'analytics')
  assert.equal(resolveSection('#missing'), 'overview')
  assert.equal(resolveSection(''), 'overview')
  assert.equal(sectionHash('batch'), '#batch')
  assert.equal(sectionHash('missing'), '#overview')
})
