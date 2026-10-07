const DISPLAY_DECIMAL_PLACES = 2

export function formatAmount(value, { places = DISPLAY_DECIMAL_PLACES } = {}) {
  if (value == null || value === '') return '—'
  const text = String(value).trim().replace(/,/g, '')
  const match = text.match(/^(-?)(\d+)(?:\.(\d+))?$/)
  if (!match) return '—'
  const sign = match[1]
  let integer = match[2].replace(/^0+(?=\d)/, '')
  let fraction = (match[3] || '').padEnd(places + 1, '0')
  if (fraction.length > places) {
    const roundDigit = Number(fraction[places])
    fraction = fraction.slice(0, places)
    if (roundDigit >= 5) {
      let carry = 1
      const digits = fraction.split('')
      for (let index = digits.length - 1; index >= 0 && carry; index -= 1) {
        const next = Number(digits[index]) + carry
        digits[index] = String(next % 10)
        carry = next >= 10 ? 1 : 0
      }
      fraction = digits.join('')
      if (carry) integer = String(BigInt(integer) + 1n)
    }
  }
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${sign}${grouped}${places ? `.${fraction.padEnd(places, '0')}` : ''}`
}

export { DISPLAY_DECIMAL_PLACES }
