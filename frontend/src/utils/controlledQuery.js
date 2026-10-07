export const CONTROLLED_SCENES = Object.freeze([
  'awardees',
  'bidders',
  'co_bidders',
  'common_buyers',
  'common_projects',
])

export function isControlledScene(scene) {
  return CONTROLLED_SCENES.includes(scene) || Object.values(SCENE_NAMES).includes(scene)
}

const SCENE_NAMES = { awardees: 'buyer_awardees', bidders: 'buyer_bidders', co_bidders: 'supplier_co_bidders' }

export function buildControlledQueryRequest(request) {
  if (!request || !isControlledScene(request.scene)) throw new Error('场景不在受控查询白名单内')
  const scene = SCENE_NAMES[request.scene] || request.scene
  const values = { ...request.filters, ...request }
  const filters = {}
  for (const key of ['start_date', 'end_date', 'min_amount', 'max_amount', 'include_winners', 'top']) {
    if (values[key] !== undefined && values[key] !== null && values[key] !== '') filters[key] = values[key]
  }
  if (values.include_awardee != null) filters.include_winners = Boolean(values.include_awardee)
  if (['buyer_awardees', 'buyer_bidders'].includes(scene) && values.buyer_id != null) filters.buyer_id = Number(values.buyer_id)
  if (scene === 'supplier_co_bidders' && values.supplier_id != null) filters.supplier_id = Number(values.supplier_id)
  if (['common_buyers', 'common_projects'].includes(scene) && Array.isArray(values.supplier_ids)) filters.supplier_ids = values.supplier_ids.map(Number)
  return {
    scene,
    filters,
    needs_clarification: Boolean(request.needs_clarification),
    clarification_options: Array.isArray(request.clarification_options) ? request.clarification_options : [],
  }
}

export function controlledQueryHeaders(datasetId) {
  return { 'Content-Type': 'application/json', 'X-Dataset-ID': datasetId || 'default' }
}
