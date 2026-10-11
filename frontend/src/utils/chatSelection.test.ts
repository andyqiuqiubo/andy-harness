/**
 * 会话页「厂商 + 模型」选择持久化的单测。
 *
 * 覆盖用户反馈的缺陷：选好厂商/模型 → 切到设置页 → 回到会话页，
 * 选择必须保留为「最后一次的选择」，而不是被打回 deepseek。
 */

import { afterEach, describe, expect, it } from 'vitest'
import {
  CHAT_MODEL_KEY,
  CHAT_PROVIDER_KEY,
  pickInitialProvider,
  readStoredModel,
  readStoredProvider,
  reconcileModel,
  storeModel,
  storeProvider,
} from './chatSelection'

function p(id: string, opts: { enabled?: boolean; has_api_key?: boolean } = {}) {
  return { id, enabled: opts.enabled ?? true, has_api_key: opts.has_api_key ?? true }
}

describe('会话页选择持久化：读写', () => {
  afterEach(() => {
    localStorage.removeItem(CHAT_PROVIDER_KEY)
    localStorage.removeItem(CHAT_MODEL_KEY)
  })

  it('读写往返：记住最后一次厂商与模型', () => {
    storeProvider('custom_oortcloud')
    storeModel('glm-5')
    expect(readStoredProvider()).toBe('custom_oortcloud')
    expect(readStoredModel()).toBe('glm-5')
  })

  it('无记录时 provider 为空、模型回退到 fallback（设置页默认模型）', () => {
    expect(readStoredProvider()).toBe('')
    expect(readStoredModel('deepseek-flash')).toBe('deepseek-flash')
  })

  it('空值不落盘，避免把空字符串写成"已选择"', () => {
    storeProvider('')
    storeModel('')
    expect(localStorage.getItem(CHAT_PROVIDER_KEY)).toBeNull()
    expect(localStorage.getItem(CHAT_MODEL_KEY)).toBeNull()
  })
})

describe('pickInitialProvider', () => {
  const providers = [
    p('deepseek'),
    p('custom_oortcloud', { has_api_key: true }),
    p('disabled-one', { enabled: false }),
  ]

  it('持久化的选择仍存在 → 保留（修复点：不再强制打回 deepseek）', () => {
    expect(pickInitialProvider('custom_oortcloud', providers)).toBe('custom_oortcloud')
  })

  it('无选择时优先 deepseek（保持首次使用者体验）', () => {
    expect(pickInitialProvider('', providers)).toBe('deepseek')
  })

  it('已选的 provider 被删除 → 回退到 deepseek', () => {
    expect(pickInitialProvider('removed-provider', providers)).toBe('deepseek')
  })

  it('无 deepseek 时取第一个可用 provider', () => {
    const noDs = [p('qwen'), p('doubao')]
    expect(pickInitialProvider('', noDs)).toBe('qwen')
  })

  it('已停用的 provider 不被保留（用户反馈：停用了还存在）', () => {
    const list = [p('custom_oortcloud'), p('deepseek', { enabled: false })]
    expect(pickInitialProvider('deepseek', list)).toBe('custom_oortcloud')
  })

  it('未配 Key 的 provider 不被保留（选中后必然调用失败）', () => {
    const list = [p('deepseek'), p('keyless', { has_api_key: false })]
    expect(pickInitialProvider('keyless', list)).toBe('deepseek')
  })

  it('持久化选择可用时仍优先保留（不被回退逻辑抢走）', () => {
    const list = [p('deepseek'), p('custom_oortcloud')]
    expect(pickInitialProvider('custom_oortcloud', list)).toBe('custom_oortcloud')
  })

  it('一个可用的都没有 → 返回空（界面呈现未配置/全部停用态）', () => {
    const noneUsable = [p('a', { has_api_key: false }), p('b', { enabled: false })]
    expect(pickInitialProvider('', noneUsable)).toBe('')
  })

  it('列表为空时不改写当前值（provider 尚未加载完）', () => {
    expect(pickInitialProvider('custom_oortcloud', [])).toBe('custom_oortcloud')
  })
})

describe('reconcileModel', () => {
  it('模型仍在该 provider 的可用列表里 → 原样保留（记住最后一次选择）', () => {
    expect(reconcileModel('custom_oortcloud', 'glm-5', ['glm-5', 'qwen3.8'])).toBe('glm-5')
  })

  it('模型已下线/不属于该 provider → 退化为列表首项', () => {
    expect(reconcileModel('deepseek', 'glm-5', ['deepseek-chat', 'deepseek-reasoner'])).toBe(
      'deepseek-chat',
    )
  })

  it('可用列表为空 → 无从校正，原样返回', () => {
    expect(reconcileModel('x', 'some-model', [])).toBe('some-model')
  })
})
