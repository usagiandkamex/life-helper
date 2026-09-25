// Same bounds as the holdings API, so an out-of-range value is explained here instead of failing with HTTP 422.
export const MAX_QUANTITY = 1_000_000_000_000
export const MAX_YEN = 1_000_000_000_000_000

/** A finite number as an integer and the power of ten it is divided by: 1.5e-8 is [15n, 9]. */
const scaled = (value: number): [bigint, number] => {
  const [mantissa, exponent = '0'] = String(value).toLowerCase().split('e')
  const [whole, fraction = ''] = mantissa.split('.')
  return [BigInt(whole + fraction), fraction.length - Number(exponent)]
}

/**
 * a + b without binary floating-point noise (0.1 + 0.2 is 0.3, not 0.30000000000000004).
 *
 * The digits are added in decimal and converted back to a number once, so no addend is rounded away first:
 * 0 + 1e-21 is 1e-21, not 0. A sum the number type cannot hold is still rounded (1 + 1e-21 is 1), which the
 * caller has to notice; totals are stored as numbers here and as floats in the API.
 */
export function addDecimal(a: number, b: number): number {
  if (!Number.isFinite(a) || !Number.isFinite(b)) return a + b
  const [digitsA, scaleA] = scaled(a)
  const [digitsB, scaleB] = scaled(b)
  const scale = Math.max(scaleA, scaleB)
  const sum = digitsA * 10n ** BigInt(scale - scaleA) + digitsB * 10n ** BigInt(scale - scaleB)
  return Number(`${sum}e${-scale}`)
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
  // A purchase too small to change the stored total would leave the save button disabled with nothing to read.
  if (next.quantity === current.quantity) return '購入数量が小さすぎて、数量の合計に反映できません'
  if (cost > 0 && next.cost === current.cost) return '購入金額が小さすぎて、取得額の合計に反映できません'
  return totalsProblem(next) ?? next
}
