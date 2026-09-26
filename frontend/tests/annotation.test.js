import test from 'node:test'
import assert from 'node:assert/strict'

import {
  annotationNoticeReviewed,
  annotationNoticeIssues,
  annotationNoticeSignature,
  annotationProgress,
  annotationTaskKey,
  validateAnnotationTask,
} from '../src/utils/annotation.js'
import { loadAnnotationSession, saveAnnotationSession } from '../src/utils/annotationStorage.js'

function taskBundle() {
  const notice = { notice_id: 'notice-a', packages: [{ package_id: 'default', items: [] }] }
  return {
    schema_version: 'bid-intel-annotation-task/1',
    task_id: 'annotator-a',
    gold: { schema_version: '1.0', status: 'draft', notices: [notice] },
    predictions: { schema_version: '1.0', status: 'predicted', notices: [structuredClone(notice)] },
    sources: [{ notice_id: 'notice-a', text: '原文', files: [] }],
  }
}

test('accepts a complete task bundle and returns it unchanged', () => {
  const bundle = taskBundle()
  assert.equal(validateAnnotationTask(bundle), bundle)
})

test('rejects a task bundle if source, gold, or prediction IDs are inconsistent', () => {
  const missingSource = taskBundle()
  missingSource.sources = []
  assert.throws(() => validateAnnotationTask(missingSource), /缺少部分公告原文/)

  const mismatch = taskBundle()
  mismatch.predictions.notices[0].notice_id = 'notice-b'
  assert.throws(() => validateAnnotationTask(mismatch), /公告清单不一致/)
})

test('per-notice review is invalidated whenever that notice changes', () => {
  const notice = taskBundle().gold.notices[0]
  const snapshots = { [notice.notice_id]: annotationNoticeSignature(notice) }
  assert.equal(annotationNoticeReviewed(notice, snapshots), true)
  assert.deepEqual(annotationProgress({ notices: [notice] }, snapshots), {
    reviewed: 1, total: 1, complete: true,
  })

  notice.packages[0].package_id = 'package-2'
  assert.equal(annotationNoticeReviewed(notice, snapshots), false)
  assert.deepEqual(annotationProgress({ notices: [notice] }, snapshots), {
    reviewed: 0, total: 1, complete: false,
  })
})

test('review is blocked for duplicate packages and incomplete rows, with actionable guidance', () => {
  const notice = taskBundle().gold.notices[0]
  notice.packages.push({
    package_id: 'default', items: [{ item_id: 'empty', product_name: null }],
    buyer: null, winners: [], bidders: [],
  })
  assert.match(annotationNoticeIssues(notice).join('\n'), /包号“default”重复/)
  assert.match(annotationNoticeIssues(notice).join('\n'), /标的物为空/)
  const snapshots = { [notice.notice_id]: annotationNoticeSignature(notice) }
  assert.equal(annotationNoticeReviewed(notice, snapshots), false)
})

test('review accepts blank undisclosed fields and valid numeric strings', () => {
  const notice = taskBundle().gold.notices[0]
  notice.packages[0].items.push({
    item_id: 'item-1', product_name: '电脑', category: null, brand: null, model: null,
    quantity: '2', unit_price: '¥1,000.00', total_price: null,
  })
  assert.deepEqual(annotationNoticeIssues(notice), [])
})

test('each annotation task gets an isolated browser draft key', () => {
  assert.equal(annotationTaskKey('pilot-a'), 'bid-intel-annotation-v1:pilot-a')
  assert.notEqual(annotationTaskKey('pilot-a'), annotationTaskKey('pilot-b'))
})

test('annotation progress falls back to localStorage when IndexedDB is unavailable', async () => {
  const oldStorage = globalThis.localStorage
  const values = new Map()
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  }
  try {
    const session = { gold: { notices: [] }, verifiedSnapshots: {} }
    const result = await saveAnnotationSession('test-session', session)
    assert.equal(result.storage, 'localstorage')
    assert.deepEqual(await loadAnnotationSession('test-session'), session)
  } finally {
    if (oldStorage === undefined) delete globalThis.localStorage
    else globalThis.localStorage = oldStorage
  }
})
