export const ANNOTATION_TASK_SCHEMA = 'bid-intel-annotation-task/1'
export const ANNOTATION_STORAGE_PREFIX = 'bid-intel-annotation-v1'

export function annotationTaskKey(taskId) {
  return `${ANNOTATION_STORAGE_PREFIX}:${String(taskId || 'manual')}`
}

export function validateAnnotationTask(value) {
  if (!value || value.schema_version !== ANNOTATION_TASK_SCHEMA) {
    throw new Error('这不是分配给你的标注任务包，请向协调员索要 task.bundle.json。')
  }
  if (!value.task_id || !value.gold || value.gold.schema_version !== '1.0' ||
      value.gold.status !== 'draft' || !Array.isArray(value.gold.notices) ||
      !value.predictions || value.predictions.schema_version !== '1.0' ||
      value.predictions.status !== 'predicted' || !Array.isArray(value.predictions.notices) ||
      !Array.isArray(value.sources)) {
    throw new Error('任务包内容不完整或版本不兼容，请重新生成任务包。')
  }
  const goldIds = value.gold.notices.map(row => row.notice_id)
  const predictionIds = value.predictions.notices.map(row => row.notice_id)
  if (!goldIds.length || new Set(goldIds).size !== goldIds.length ||
      goldIds.length !== predictionIds.length ||
      goldIds.some((id, index) => id !== predictionIds[index])) {
    throw new Error('任务包内的 Gold 和预测公告清单不一致，请找协调员重发。')
  }
  const sourceIds = new Set(value.sources.map(row => row.notice_id))
  if (goldIds.some(id => !sourceIds.has(id))) {
    throw new Error('任务包缺少部分公告原文，请找协调员重发。')
  }
  return value
}

export function annotationNoticeSignature(notice) {
  return JSON.stringify(notice)
}

const ITEM_FIELDS = [
  'product_name', 'category', 'brand', 'model', 'quantity', 'unit_price', 'total_price',
]

function hasValue(value) {
  return value !== null && value !== undefined && String(value).trim() !== ''
}

function validNumber(value, monetary = false) {
  if (!hasValue(value)) return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value !== 'string') return false
  let text = value.normalize('NFKC').replace(/\s+/g, '')
  if (monetary) {
    text = text.replace(/^(?:人民币|CNY|RMB|¥)/i, '')
      .replace(/(?:人民币|CNY|RMB|元)$/i, '')
    if (/[万亿]$/.test(text)) text = text.slice(0, -1)
  }
  return /^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(text)
}

export function annotationNoticeIssues(notice) {
  if (!notice || !Array.isArray(notice.packages)) return ['采购包列表缺失，请联系协调员重新发送任务包。']
  const issues = []
  const seenIds = new Set()
  notice.packages.forEach((packageRow, packageIndex) => {
    const label = `第 ${packageIndex + 1} 个包`
    const id = typeof packageRow.package_id === 'string' ? packageRow.package_id : ''
    if (!id.trim()) issues.push(`${label}的包号为空；请按原文填写，未分包时填 default。`)
    else if (id !== id.trim()) issues.push(`${label}的包号首尾有空格；请删除空格。`)
    else if (seenIds.has(id)) issues.push(`包号“${id}”重复；每个包必须有不同包号。`)
    seenIds.add(id)

    for (const [itemIndex, item] of (packageRow.items || []).entries()) {
      if (!ITEM_FIELDS.some(field => hasValue(item[field]))) {
        issues.push(`${label}第 ${itemIndex + 1} 行标的物为空；补填一个字段或删除空行。`)
      }
      for (const field of ['quantity', 'unit_price', 'total_price']) {
        if (!validNumber(item[field], field !== 'quantity')) {
          issues.push(`${label}第 ${itemIndex + 1} 行的${field === 'quantity' ? '数量' : field === 'unit_price' ? '单价' : '总价'}只能填数字。`)
        }
      }
    }
    if (packageRow.buyer && !hasValue(packageRow.buyer.name)) {
      issues.push(`${label}的采购单位名称为空；补填名称或删除此项。`)
    }
    for (const [role, title] of [['winners', '中标方'], ['bidders', '投标方']]) {
      for (const [personIndex, person] of (packageRow[role] || []).entries()) {
        if (!hasValue(person.name)) issues.push(`${label}第 ${personIndex + 1} 个${title}名称为空。`)
        if (role === 'winners' && !validNumber(person.award_amount, true)) {
          issues.push(`${label}第 ${personIndex + 1} 个中标方的金额只能填数字。`)
        }
      }
    }
  })
  return issues
}

export function annotationNoticeReviewed(notice, snapshots = {}) {
  return Boolean(notice && annotationNoticeIssues(notice).length === 0 &&
    snapshots[notice.notice_id] === annotationNoticeSignature(notice))
}

export function annotationProgress(gold, snapshots = {}) {
  const notices = gold?.notices || []
  const reviewed = notices.filter(notice => annotationNoticeReviewed(notice, snapshots)).length
  return { reviewed, total: notices.length, complete: notices.length > 0 && reviewed === notices.length }
}
