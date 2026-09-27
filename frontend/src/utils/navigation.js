export const navigationGroups = [
  { label: '工作台', items: [{ id: 'overview', label: '首页概览' }] },
  { label: '数据接入', items: [{ id: 'import', label: '单条导入' }, { id: 'batch', label: '批量处理' }] },
  { label: '分析应用', items: [{ id: 'items', label: '标的物检索' }, { id: 'analytics', label: '主体关系分析' }, { id: 'quality', label: '质量评测' }] },
  { label: '系统设置', items: [{ id: 'datasets', label: '数据集管理' }, { id: 'model', label: '模型配置' }] },
]

const sectionIds = new Set(navigationGroups.flatMap((group) => group.items.map((item) => item.id)))

export function resolveSection(value = '') {
  const section = String(value).replace(/^#/, '')
  return sectionIds.has(section) ? section : 'overview'
}

export function sectionHash(section) {
  return `#${resolveSection(section)}`
}
