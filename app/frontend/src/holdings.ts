// Same bounds as the holdings API, so an out-of-range value is explained here instead of failing with HTTP 422.
export const MAX_QUANTITY = 1_000_000_000_000
export const MAX_YEN = 1_000_000_000_000_000

const fractionDigits = (value: number) => {
  const [mantissa, exponent = '0'] = String(value).toLowerCase().split('e')
  return Math.max(0, (mantissa.split('.')[1] ?? '').length - Number(exponent))
}

/** a + b without binary floating-point noise (0.1 + 0.2 is 0.3, not 0.30000000000000004). */
export function addDecimal(a: number, b: number): number {
  const digits = Math.min(20, Math.max(fractionDigits(a), fractionDigits(b)))
  return Number((a + b).toFixed(digits))
}

/** Reads a number input. Blank or non-numeric text is NaN, never 0. */
export function parseNumber(text: string): number {
  return text.trim() === '' ? Number.NaN : Number(text)
}

/** Cost per price unit (usually 10,000 units for a fund, one share for a stock); null when nothing is held. */
export function averageCost(cost: number, quantity: number, priceUnit: number): number | null {
  return quantity > 0 ? (cost / quantity) * priceUnit : null
}

export type Totals = { quantity: number; cost: number }

/** Why the totals cannot be saved, or null when they can. */
export function totalsProblem({ quantity, cost }: Totals): string | null {
  if (!Number.isFinite(quantity) || quantity < 0) return '数量には 0 以上の数値を入力してください'
  if (quantity > MAX_QUANTITY) return '数量が大きすぎます'
  if (!Number.isFinite(cost) || cost < 0) return '取得額には 0 以上の数値を入力してください'
  if (cost > MAX_YEN) return '取得額が大きすぎます'
  return null
}

/** Totals after a purchase, or the reason the purchase cannot be added. */
export function afterPurchase(current: Totals, quantity: number, cost: number): Totals | string {
  if (!Number.isFinite(quantity) || quantity <= 0) return '購入数量には 0 より大きい数値を入力してください'
  if (!Number.isFinite(cost) || cost < 0) return '購入金額には 0 以上の数値を入力してください'
  const next = { quantity: addDecimal(current.quantity, quantity), cost: addDecimal(current.cost, cost) }
  return totalsProblem(next) ?? next
}
