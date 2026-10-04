// 入力の途中の内容（下書き）。ほかの会話・ファイル・タブに移っても残し、戻ったときに続きから書けるようにする。
// 画面の中だけに持ち、再読み込みやログアウトで消す（添付ファイルや機微な内容をブラウザに保存しない）。
const stores: Map<string, unknown>[] = []
// ログアウトで下書きを消すたびに進める。それより前に始めた処理が、終わったあとで下書きを書き戻さないようにする。
let epoch = 0

export function draftStore<T>(): Map<string, T> {
  const store = new Map<string, T>()
  stores.push(store)
  return store
}

export function draftEpoch() {
  return epoch
}

export function clearDrafts() {
  epoch += 1
  for (const store of stores) store.clear()
}
