import { describe, expect, it } from 'vitest'
import { formatMs, formatTokens } from './format'

describe('formatTokens', () => {
  it('小数字用千分位逗号', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(123)).toBe('123')
    expect(formatTokens(1234)).not.toBe('1,234') // 1234 已进 K 档
  })

  it('>=1K 用 K 并去掉多余 .0', () => {
    expect(formatTokens(1000)).toBe('1K')
    expect(formatTokens(1500)).toBe('1.5K')
    expect(formatTokens(12_000)).toBe('12K')
  })

  it('>=1M 用 M', () => {
    expect(formatTokens(1_000_000)).toBe('1M')
    expect(formatTokens(2_500_000)).toBe('2.5M')
  })

  it('空值 / 非法值按 0 处理', () => {
    expect(formatTokens(undefined)).toBe('0')
    expect(formatTokens(null)).toBe('0')
    expect(formatTokens(Number.NaN)).toBe('0')
  })
})

describe('formatMs', () => {
  it('小于 1 秒用 ms', () => {
    expect(formatMs(0)).toBe('0 ms')
    expect(formatMs(999)).toBe('999 ms')
  })

  it('大于等于 1 秒用 s', () => {
    expect(formatMs(1000)).toBe('1.00 s')
    expect(formatMs(2500)).toBe('2.50 s')
  })

  it('空值按 0 处理', () => {
    expect(formatMs(undefined)).toBe('0 ms')
  })
})
