// A stale response must never populate a newly selected dataset, including A→B→A.
export function createDatasetClient(getDataset, getVersion, fetchImpl = fetch) {
  return async (url, options = {}) => {
    const version = getVersion()
    const headers = new Headers(options.headers)
    headers.set('X-Dataset-ID', getDataset())
    const checkCurrent = () => {
      if (version !== getVersion()) throw new DOMException('数据集已切换', 'AbortError')
    }
    let response
    try {
      response = await fetchImpl(url, { ...options, headers })
    } catch (error) {
      checkCurrent()
      throw error
    }
    checkCurrent()
    return {
      ok: response.ok,
      async json() {
        let payload
        try {
          payload = await response.json()
        } catch (error) {
          checkCurrent()
          throw error
        }
        checkCurrent()
        return payload
      },
    }
  }
}
