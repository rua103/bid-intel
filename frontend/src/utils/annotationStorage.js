const DATABASE_NAME = 'bid-intel-annotation'
const STORE_NAME = 'sessions'
const LAST_SESSION_KEY = 'bid-intel-annotation-last-session'
const LEGACY_SESSION_KEY = 'bid-intel-annotation-v1'

function openDatabase() {
  if (!globalThis.indexedDB) return Promise.reject(new Error('IndexedDB is unavailable'))
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, 1)
    request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME)
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('IndexedDB open failed'))
  })
}

function readFromStore(key) {
  return openDatabase().then(database => new Promise((resolve, reject) => {
    const request = database.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).get(key)
    request.onsuccess = () => { database.close(); resolve(request.result || null) }
    request.onerror = () => { database.close(); reject(request.error || new Error('IndexedDB read failed')) }
  }))
}

function writeToStore(key, value) {
  return openDatabase().then(database => new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, 'readwrite')
    transaction.objectStore(STORE_NAME).put(value, key)
    transaction.oncomplete = () => { database.close(); resolve() }
    transaction.onerror = () => { database.close(); reject(transaction.error || new Error('IndexedDB write failed')) }
    transaction.onabort = () => { database.close(); reject(transaction.error || new Error('IndexedDB write aborted')) }
  }))
}

export function lastAnnotationSessionKey() {
  try { return localStorage.getItem(LAST_SESSION_KEY) || LEGACY_SESSION_KEY }
  catch { return LEGACY_SESSION_KEY }
}

export async function loadAnnotationSession(key) {
  try {
    const saved = await readFromStore(key)
    if (saved) return saved
  } catch { /* Older browsers and private browsing fall back to localStorage. */ }
  try {
    const raw = localStorage.getItem(key) || (key === LEGACY_SESSION_KEY ? null : null)
    return raw ? JSON.parse(raw) : null
  } catch { return null }
}

export async function saveAnnotationSession(key, value) {
  try {
    await writeToStore(key, value)
    try { localStorage.setItem(LAST_SESSION_KEY, key) } catch { /* Data itself lives in IndexedDB. */ }
    return { storage: 'indexeddb' }
  } catch (indexedDbError) {
    try {
      localStorage.setItem(key, JSON.stringify(value))
      localStorage.setItem(LAST_SESSION_KEY, key)
      return { storage: 'localstorage' }
    } catch (localStorageError) {
      const error = new Error('浏览器无法保存草稿，请立即下载进度备份。')
      error.cause = localStorageError || indexedDbError
      throw error
    }
  }
}

export const LEGACY_ANNOTATION_SESSION_KEY = LEGACY_SESSION_KEY
