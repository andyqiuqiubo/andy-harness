/** 数字/时间格式化工具（共享）。 */

/** token 数字：>=1M 用 M，>=1K 用 K，否则千分位逗号。 */
export function formatTokens(n: number | undefined | null): string {
  const v = Number(n) || 0
  if (v >= 1_000_000)
    return (v / 1_000_000).toFixed(2).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1') + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return v.toLocaleString('en-US')
}

/** 毫秒耗时：<1s 用 ms，否则用 s。 */
export function formatMs(n: number | undefined | null): string {
  const v = Number(n) || 0
  if (v < 1000) return `${v} ms`
  return `${(v / 1000).toFixed(2)} s`
}
