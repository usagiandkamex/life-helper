import type { ReactNode, RefObject } from 'react'

// 長い入力欄の「すべて選択」。スマホの長押しでは「すべて選択」が出ないことがあるので、ボタンでも選べるようにする。
export function SelectAllButton({
  target,
  className = 'button small',
  title,
  children = 'すべて選択',
}: {
  target: RefObject<HTMLTextAreaElement | null>
  className?: string
  title?: string
  children?: ReactNode
}) {
  return (
    <button
      type="button"
      className={className}
      title={title}
      onClick={() => {
        const el = target.current
        if (!el) return
        el.focus()
        // select() では iPhone で選択されないことがあるので、範囲を指定して選ぶ。
        el.setSelectionRange(0, el.value.length)
      }}
    >
      {children}
    </button>
  )
}
