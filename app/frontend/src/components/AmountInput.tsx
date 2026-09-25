import { useEffect, useRef, useState, type ChangeEvent, type InputHTMLAttributes } from 'react'
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
  const initial = typeof defaultValue === 'number' ? groupNumber(defaultValue) : groupDigits(defaultValue ?? '') ?? ''
  const [text, setText] = useState(initial)
  // 打ち直してもらうときに戻す値（変換中の打ちかけの文字は入れない）。
  const accepted = useRef(initial)
  // 変換中（IME）は打ちかけの文字をそのままにして、確定してから区切りを入れる。
  const composing = useRef(false)
  const controlled = value !== undefined
  const shown = controlled ? value : text
  // 親が値を入れ替えることもあるので、変換中でなければ、いま出ている値を戻り先にしておく。
  useEffect(() => {
    if (!composing.current) accepted.current = shown
  })
  const update = (next: string) => {
    if (!controlled) setText(next)
    onValueChange?.(next)
  }
  const format = (input: HTMLInputElement) => {
    const caretAt = input.selectionStart ?? input.value.length
    const formatted = groupDigits(input.value)
    if (formatted === null) {
      // 数字にならない文字は受け付けず、打つ前の値に戻す（"-500" が 500 になるような読み替えをしない）。
      const added = Math.max(input.value.length - accepted.current.length, 0)
      input.value = accepted.current
      const caret = Math.min(Math.max(caretAt - added, 0), accepted.current.length)
      input.setSelectionRange(caret, caret)
      update(accepted.current)
      return
    }
    const typed = (groupDigits(input.value.slice(0, caretAt)) ?? '').replace(/,/g, '').length
    input.value = formatted
    // 区切りが増えるとカーソルが末尾に飛ぶので、打った桁数のうしろに置き直す。
    let caret = 0
    for (let digits = 0; caret < formatted.length && digits < typed; caret += 1) {
      if (formatted[caret] !== ',') digits += 1
    }
    input.setSelectionRange(caret, caret)
    accepted.current = formatted
    update(formatted)
  }
  return (
    <input
      type="text"
      inputMode="decimal"
      autoComplete="off"
      {...rest}
      value={shown}
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
