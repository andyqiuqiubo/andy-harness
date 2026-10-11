/**
 * 会话页「厂商 + 模型」选择的持久化。
 *
 * 背景（用户反馈的真实缺陷）：在会话页选好厂商（如 OortCloud）与具体模型后，
 * 切到设置页再回来，选择被打回 deepseek。原因有二：
 *  1. `ChatView` 未做 KeepAlive，切页即卸载重建，组件内的
 *     `selectedProvider` / `selectedModel` 会重新初始化；
 *  2. 旧实现的 provider 初值恒为 `''`，重建后被「优先 deepseek」逻辑接管，
 *     `selectedModel` 又被「校正为该 provider 的首个可用模型」连带改写。
 * 结论：最后一次选择必须落盘，重建时优先恢复。
 *
 * 为什么不复用 settings 的 `session_model`：它是「设置页 → 默认模型」语义
 * （还会作为 default_model 推给后端），与会话页的临时选择不是一回事，
 * 共用会互相覆盖。故这里使用独立的 key。
 */

import { modelsForProvider } from './modelCatalog'
import { usableProviders, type ProviderLike } from './providerFilter'

export type { ProviderLike }

/** 会话页所选厂商的 localStorage key。 */
export const CHAT_PROVIDER_KEY = 'chat_selected_provider'
/** 会话页所选模型的 localStorage key。 */
export const CHAT_MODEL_KEY = 'chat_selected_model'

/** 读取 localStorage 的容错封装（隐私模式 / 存储被禁用时退化为空）。 */
function safeGet(key: string): string {
  try {
    return localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

function safeSet(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(key, value)
  } catch {
    // 隐私模式 / 存储被禁用：忽略即可，行为退化为「每次重新选择」
  }
}

/** 上次选择的厂商 id（无记录返回 ''）。 */
export function readStoredProvider(): string {
  return safeGet(CHAT_PROVIDER_KEY)
}

/** 上次选择的模型 id；无记录时回退到 fallback（如设置页的默认模型）。 */
export function readStoredModel(fallback = ''): string {
  return safeGet(CHAT_MODEL_KEY) || fallback
}

/** 记录厂商选择。 */
export function storeProvider(providerId: string): void {
  safeSet(CHAT_PROVIDER_KEY, providerId)
}

/** 记录模型选择。 */
export function storeModel(modelId: string): void {
  safeSet(CHAT_MODEL_KEY, modelId)
}

/**
 * 决定会话页初始应选中的厂商。
 *
 * 优先级（高 → 低）：
 *  1. 当前 / 持久化的选择**仍可用**（已启用 + 已配 Key）→ 保留；
 *  2. 否则优先 deepseek（保持首次使用者的既有体验）；
 *  3. 否则第一个可用厂商；
 *  4. 一个都没有 → 返回 ''（界面呈现「未配置 / 全部停用」态）。
 *
 * 关键：判定用「可用」而非「存在」。会话页的厂商选框只列出可用厂商
 * （见 providerFilter），若这里还保留一个已停用 / 缺 Key 的选择，选框会显示空白，
 * 而发送时却带着一个不可用的 provider_id —— 用户反馈「停用的还存在」正是此类。
 */
export function pickInitialProvider(
  currentOrStoredId: string,
  providers: ReadonlyArray<ProviderLike>,
): string {
  // 列表尚未加载完：无从判定可用性，保持现状（避免把已恢复的选择提前清掉）
  if (providers.length === 0) return currentOrStoredId
  const usable = usableProviders(providers)
  if (currentOrStoredId && usable.some((p) => p.id === currentOrStoredId)) {
    return currentOrStoredId
  }
  const deepseek = usable.find((p) => p.id === 'deepseek' || p.id.includes('deepseek'))
  if (deepseek) return deepseek.id
  return usable.length > 0 ? usable[0].id : ''
}

/**
 * 校正模型选择。
 *
 * localStorage / 上次选择里可能存着该 provider 已下线的旧模型名，不校正会让
 * 下拉显示空白、发消息时 404。规则：模型仍在该 provider 的可用列表里 → 保留
 * （这是「记住用户最后一次选择」的关键）；否则退化为列表首项；列表为空时
 * 无从校正，原样返回。
 */
export function reconcileModel(
  providerId: string,
  currentModel: string,
  providerModelIds: string[],
): string {
  const available = modelsForProvider(providerId, providerModelIds)
  if (available.length === 0) return currentModel
  return available.some((o) => o.id === currentModel) ? currentModel : available[0].id
}
