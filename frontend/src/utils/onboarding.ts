import type { Provider } from '../api/types'

/**
 * 首次使用引导：判断是否需要提示配置 API Key，并挑选要引导的 provider。
 *
 * 触发标准（全部满足）：
 * - provider 列表已加载（非空）；
 * - 没有任何「已启用且已配置 Key」的 provider——此时用户发消息必然失败；
 * - 未开启模拟模式（模拟模式下无需 Key 也能体验）。
 */

export interface OnboardingTarget {
  /** 是否需要展示引导 */
  needed: boolean
  /** 引导的目标 provider（优先 deepseek；列表里没有则 null，引导去设置页） */
  provider: Provider | null
}

export function pickOnboardingTarget(
  providers: Provider[],
  mockMode: boolean,
): OnboardingTarget {
  if (mockMode || providers.length === 0) {
    return { needed: false, provider: null }
  }
  const usable = providers.some((p) => p.enabled !== false && p.has_api_key)
  if (usable) {
    return { needed: false, provider: null }
  }
  const deepseek =
    providers.find((p) => p.id === 'deepseek') ||
    providers.find((p) => p.id.includes('deepseek')) ||
    null
  return { needed: true, provider: deepseek }
}
