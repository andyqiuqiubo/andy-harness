import { describe, expect, it } from 'vitest'
import { modelsForProvider, MODEL_CATALOG } from './modelCatalog'

describe('modelsForProvider', () => {
  it('deepseek：只显示目录内两个模型，显示名为产品口径', () => {
    const opts = modelsForProvider('deepseek', [
      'deepseek-flash',
      'deepseek-v4-pro',
      'deepseek-chat',
      'deepseek-reasoner',
    ])
    expect(opts).toHaveLength(2)
    expect(opts[0]).toEqual({ id: 'deepseek-flash', label: 'DEEPSEEK-V4.1-FLASH' })
    expect(opts[1]).toEqual({ id: 'deepseek-v4-pro', label: 'DEEPSEEK-V4-PRO' })
  })

  it('qwen：显示 QWEN-MAX / QWEN-PLUS（按目录顺序）', () => {
    const opts = modelsForProvider('qwen', ['qwen-turbo', 'qwen-plus', 'qwen-max', 'qwen-long'])
    expect(opts.map((o) => o.label)).toEqual(['QWEN-MAX', 'QWEN-PLUS'])
  })

  it('doubao：显示 DOUBAO-PRO-32K / DOUBAO-LITE-4K', () => {
    const opts = modelsForProvider('doubao', ['doubao-pro-4k', 'doubao-pro-32k', 'doubao-lite-4k'])
    expect(opts.map((o) => o.label)).toEqual(['DOUBAO-PRO-32K', 'DOUBAO-LITE-4K'])
  })

  it('目录与可用列表完全对不上时回落为全部可用模型（大写显示）', () => {
    const opts = modelsForProvider('deepseek', ['some-new-model'])
    expect(opts).toEqual([{ id: 'some-new-model', label: 'SOME-NEW-MODEL' }])
  })

  it('自定义 provider（不在目录中）：全部可用模型大写显示', () => {
    const opts = modelsForProvider('custom_x', ['glm-5', 'kimi-k2'])
    expect(opts).toEqual([
      { id: 'glm-5', label: 'GLM-5' },
      { id: 'kimi-k2', label: 'KIMI-K2' },
    ])
  })

  it('无可用模型时返回空数组', () => {
    expect(modelsForProvider('deepseek', [])).toEqual([])
  })

  it('目录内 id 与显示名一一对应且无重复', () => {
    for (const [pid, list] of Object.entries(MODEL_CATALOG)) {
      const ids = list.map((m) => m.id)
      const labels = list.map((m) => m.label)
      expect(new Set(ids).size, `${pid} id 重复`).toBe(ids.length)
      expect(new Set(labels).size, `${pid} label 重复`).toBe(labels.length)
    }
  })
})
