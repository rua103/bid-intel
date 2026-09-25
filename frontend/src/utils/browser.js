let fallbackIdCounter = 0

// Annotation identifiers are local record keys, not security tokens.
export function annotationId(prefix, cryptoApi = globalThis.crypto) {
  if (typeof cryptoApi?.randomUUID === 'function') return `${prefix}-${cryptoApi.randomUUID()}`
  fallbackIdCounter += 1
  const randomPart = Math.random().toString(36).slice(2, 10).padEnd(8, '0')
  return `${prefix}-${Date.now().toString(36)}-${fallbackIdCounter.toString(36)}-${randomPart}`
}

export function resolveApiBase(configured, location) {
  const override = configured?.trim()
  if (override) return override.replace(/\/+$/, '')
  const hostname = location.hostname.includes(':') && !location.hostname.startsWith('[')
    ? `[${location.hostname}]` : location.hostname
  return `${location.protocol}//${hostname}:8000`
}
