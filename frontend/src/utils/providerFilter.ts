/**
 * Provider 可用性判定（会话 / 评测 / 工作流等所有「选模型」场景共用）。
 *
 * 判定标准与设置页语义一致：**已启用且已配置 API Key** 才算可用。
 * 抽出来的原因：`EvalLabView` 里曾内联过同一条件，而 `ChatView` / `WorkflowView`
 * 漏了过滤，导致**已停用 / 未配 Key 的 provider 仍出现在选框里**（用户反馈：
 * 「停用或者被删除的模型不应该出现在会话界面的选框里，目前是停用了也存在」）。
 * 统一到一处后，三处不会再有偏差。
 */

/** 判定所需的最小 provider 结构（`Provider` 天然满足）。 */
export interface ProviderLike {
  id: string
  enabled?: boolean
  has_api_key?: boolean
}

/**
 * provider 是否可用于对话 / 推理。
 *
 * - `enabled === false` → 设置页被停用，不可用；
 * - `has_api_key` 为假 → 缺密钥，调用必然失败，不可用；
 * - 注意：已**删除**的 provider 根本不会出现在 provider 列表里，无需此函数处理。
 */
export function isUsableProvider(p: ProviderLike): boolean {
  return p.enabled !== false && Boolean(p.has_api_key)
}

/** 过滤出可用于对话 / 推理的 provider（保持原顺序）。 */
export function usableProviders<T extends ProviderLike>(list: readonly T[]): T[] {
  return list.filter(isUsableProvider)
}
