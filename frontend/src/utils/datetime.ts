/**
 * 时间显示工具。
 *
 * 后端新能力（traces / memories / artifacts）统一以 **UTC** 存储时间戳，
 * 且带显式时区偏移（形如 `2026-09-28T01:30:00+00:00`）。这里统一转成本地时间展示，
 * 否则会出现「日期对、时分秒差 8 小时」的问题。
 *
 * 兼容两种输入：
 * - 带时区（`Z` / `+08:00`）：直接交给 Date 解析；
 * - 无时区（`YYYY-MM-DD HH:MM:SS`）：按 UTC 解析（新能力的存储约定）。
 */

export function parseServerTime(ts?: string | null): Date | null {
  if (!ts) return null
  const raw = ts.trim()
  if (!raw) return null
  const normalized = raw.includes('T') ? raw : raw.replace(' ', 'T')
  const hasTz = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(normalized)
  const d = new Date(hasTz ? normalized : `${normalized}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** 格式化为本地时间：YYYY-MM-DD HH:mm:ss（默认含秒）。无法解析时原样返回。 */
export function formatLocalTime(
  ts?: string | null,
  options: { seconds?: boolean } = {},
): string {
  const d = parseServerTime(ts)
  if (!d) return ts ?? ''
  const seconds = options.seconds ?? true
  const base = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`
  return seconds ? `${base}:${pad(d.getSeconds())}` : base
}
