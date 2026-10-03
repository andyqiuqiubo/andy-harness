import { describe, expect, it } from 'vitest'
import { formatLocalTime, parseServerTime } from './datetime'

describe('parseServerTime', () => {
  it('空值返回 null', () => {
    expect(parseServerTime(null)).toBeNull()
    expect(parseServerTime(undefined)).toBeNull()
    expect(parseServerTime('   ')).toBeNull()
  })

  it('带 Z 的 UTC 时间可解析', () => {
    const d = parseServerTime('2026-09-28T01:30:00Z')
    expect(d).toBeInstanceOf(Date)
    expect(Number.isNaN(d!.getTime())).toBe(false)
  })

  it('带显式偏移可解析', () => {
    const d = parseServerTime('2026-09-28T09:30:00+08:00')
    expect(d).toBeInstanceOf(Date)
  })

  it('无时区的"YYYY-MM-DD HH:mm:ss"按 UTC 解析', () => {
    const withT = parseServerTime('2026-09-28 01:30:00')
    const expected = parseServerTime('2026-09-28T01:30:00Z')
    expect(withT?.getTime()).toBe(expected?.getTime())
  })

  it('非法字符串返回 null', () => {
    expect(parseServerTime('not-a-date')).toBeNull()
  })
})

describe('formatLocalTime', () => {
  it('格式为 YYYY-MM-DD HH:mm:ss（本地时区）', () => {
    const s = formatLocalTime('2026-09-28T01:30:45Z')
    expect(s).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  })

  it('seconds=false 时不含秒', () => {
    const s = formatLocalTime('2026-09-28T01:30:45Z', { seconds: false })
    expect(s).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
  })

  it('无法解析时原样返回', () => {
    expect(formatLocalTime('garbage')).toBe('garbage')
    expect(formatLocalTime(null)).toBe('')
  })
})
