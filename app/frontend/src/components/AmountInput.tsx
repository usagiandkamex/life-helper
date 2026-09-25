import { useRef, useState, type ChangeEvent, type InputHTMLAttributes } from 'react'
import { groupDigits, groupNumber } from '../holdings'

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, 'defaultValue' | 'onChange' | 'type' | 'value'> & {
  /** 送信時にまとめて読む入力の初期値。 */
  defaultValue?: number | string
  /** 入力中の値を親が持つとき（打った値ですぐ計算するときなど）に指定する。 */
  value?: string
  onValueChange?: (value: string) => void
}

/**
 * 金額・数量の入力欄。打つたびに 3 桁区切りを入れる（type="number" では区切りを表示できない）。
 * 値は parseNumber で読む。
 */
export function AmountInput({ defaultValue, value, onValueChange, ...rest }: Props) {
  const [text, setText] = useState(() =>
    typeof defaultValue === 'number' ? groupNumber(defaultValue) : groupDigits(defaultValue ?? ''),
  )
  // 変換中（IME）は打ちかけの文字をそのままにして、確定してから区切りを入れる。
  const composing = useRef(false)
  const controlled = value !== undefined
  const update = (next: string) => {
    if (!controlled) setText(next)
    onValueChange?.(next)
  }
  const format = (input: HTMLInputElement) => {
    const typed = groupDigits(input.value.slice(0, input.selectionStart ?? input.value.length)).replace(/,/g, '').length
    const formatted = groupDigits(input.value)
    input.value = formatted
    // 区切りが増えるとカーソルが末尾に飛ぶので、打った桁数のうしろに置き直す。
    let caret = 0
    for (let digits = 0; caret < formatted.length && digits < typed; caret += 1) {
      if (formatted[caret] !== ',') digits += 1
    }
    input.setSelectionRange(caret, caret)
    update(formatted)
  }
  return (
    <input
      type="text"
      inputMode="decimal"
      autoComplete="off"
      {...rest}
      value={controlled ? value : text}
      onChange={(e: ChangeEvent<HTMLInputElement>) => (composing.current ? update(e.target.value) : format(e.target))}
      onCompositionStart={() => {
        composing.current = true
      }}
      onCompositionEnd={(e) => {
        composing.current = false
        format(e.currentTarget)
      }}
    />
  )
}
