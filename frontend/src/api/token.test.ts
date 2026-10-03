/**
 * E12：token 本地持久化测试。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { clearToken, getToken, setToken } from './token'

describe('token storage', () => {
  beforeEach(() => {
    clearToken()
  })

  it('empty by default', () => {
    expect(getToken()).toBe('')
  })

  it('persists and reads token', () => {
    setToken('abc.def.ghi')
    expect(getToken()).toBe('abc.def.ghi')
  })

  it('clearToken removes it', () => {
    setToken('xyz')
    clearToken()
    expect(getToken()).toBe('')
  })

  it('setToken empty string removes it', () => {
    setToken('xyz')
    setToken('')
    expect(getToken()).toBe('')
  })
})
