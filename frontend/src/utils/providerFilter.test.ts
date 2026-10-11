/**
 * Provider 可用性判定单测。
 *
 * 判定标准：**已启用且已配置 API Key** 才可用于对话 / 推理。
 * 会话页、工作流 LLM 节点、评测页的厂商选框都应只列可用项。
 */

import { describe, expect, it } from 'vitest'
import { isUsableProvider, usableProviders } from './providerFilter'

describe('isUsableProvider', () => {
  it('已启用且已配 Key → 可用', () => {
    expect(isUsableProvider({ id: 'a', enabled: true, has_api_key: true })).toBe(true)
  })

  it('已停用（enabled=false）→ 不可用', () => {
    expect(isUsableProvider({ id: 'a', enabled: false, has_api_key: true })).toBe(false)
  })

  it('未配 Key → 不可用', () => {
    expect(isUsableProvider({ id: 'a', enabled: true, has_api_key: false })).toBe(false)
  })

  it('enabled 缺省（后端未返回该字段）时按启用处理', () => {
    expect(isUsableProvider({ id: 'a', has_api_key: true })).toBe(true)
  })

  it('既停用又缺 Key → 不可用', () => {
    expect(isUsableProvider({ id: 'a', enabled: false, has_api_key: false })).toBe(false)
  })
})

describe('usableProviders', () => {
  it('过滤掉停用与缺 Key 的项，并保持原顺序', () => {
    const list = [
      { id: 'deepseek', enabled: false, has_api_key: true },
      { id: 'custom_oortcloud', enabled: true, has_api_key: true },
      { id: 'keyless', enabled: true, has_api_key: false },
      { id: 'qwen', enabled: true, has_api_key: true },
    ]
    expect(usableProviders(list).map((p) => p.id)).toEqual(['custom_oortcloud', 'qwen'])
  })

  it('不修改入参（纯函数）', () => {
    const list = [{ id: 'a', enabled: false, has_api_key: true }]
    usableProviders(list)
    expect(list).toHaveLength(1)
  })

  it('全部不可用时返回空数组', () => {
    expect(usableProviders([{ id: 'a', enabled: false, has_api_key: true }])).toEqual([])
  })
})
