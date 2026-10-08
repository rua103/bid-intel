import { formatAmount } from './amountPolicy.js'

export const outcomeLabel = (value) => ({
  winner: '中标',
  nonwinner: '未中标',
  unknown: '结果未披露',
}[String(value || '').toLowerCase()] || value || '结果未披露')

export const supportsParticipationFilter = (scene) =>
  ['bidders', 'buyer_bidders', 'co_bidders', 'supplier_co_bidders'].includes(scene)

export function participationScopeLabel(payload) {
  if (!payload) return '未返回参与口径'
  const scene = payload.scene ?? payload.request?.scene
  if (['awardees', 'buyer_awardees', 'common_buyers'].includes(scene)) return '按中标记录统计'
  const outcomes = payload.participation_outcomes
    ?? payload.semantics?.participation_outcomes
    ?? payload.query_semantics?.participation_outcomes
    ?? payload.payload?.participation_outcomes
  if (Array.isArray(outcomes) && outcomes.length) return outcomes.map(outcomeLabel).join('、')
  if (scene === 'common_projects') return '包含中标、未中标和结果未披露'
  const includeWinners = payload.filters?.include_winners
    ?? payload.include_winners
    ?? payload.include_awardee
    ?? payload.request?.filters?.include_winners
    ?? payload.payload?.include_winners
  if (includeWinners === true) return '包含中标、未中标和结果未披露'
  if (includeWinners === false) return '仅统计未中标；不包含中标和结果未披露'
  return '未返回参与口径'
}

export function productSupplierMetrics(row) {
  if (typeof row === 'string' || row?.legacy) return ''
  const parts = []
  if (row?.project_count != null) parts.push(`${row.project_count} 个项目`)
  if (row?.package_count != null) parts.push(`${row.package_count} 个采购包`)
  if (row?.amount_total != null && row.amount_total !== '') {
    parts.push(`已披露标的总价合计 ¥${formatAmount(row.amount_total)}`)
  } else {
    parts.push('未披露标的总价')
  }
  return parts.join(' · ')
}

const nonemptyText = (value) => typeof value === 'string' && Boolean(value.trim())

export function productSupplierEvidence(row) {
  const entries = Array.isArray(row?.evidence) ? row.evidence : []
  const records = [row, ...entries]
  if (records.some((entry) => nonemptyText(entry?.source_evidence))) return '有记录级证据片段'
  if (records.some((entry) => nonemptyText(entry?.source_file) || nonemptyText(entry?.source_location))) {
    return '有来源定位，未保存证据片段'
  }
  return '未保存来源证据'
}
