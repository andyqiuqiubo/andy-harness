/**
 * 模型目录：每个 provider 在会话页模型下拉里**只展示**目录内的模型。
 *
 * 规则（curatedModelsFor）：
 * - provider 在目录中：展示目录条目 ∩ 该 provider 可用模型（保持目录顺序）；
 *   交集为空（如 provider 配置的模型列表与目录完全对不上）则回落展示全部可用模型；
 * - provider 不在目录中：回落展示全部可用模型（大写显示）。
 */

export interface CatalogModel {
  /** 发送给后端 / 模型 API 的真实模型 id */
  id: string
  /** 下拉里显示的名称 */
  label: string
}

export const MODEL_CATALOG: Record<string, CatalogModel[]> = {
  deepseek: [
    // API 实际模型名是 deepseek-flash（V4.1 Flash 代次），显示名按产品口径
    { id: 'deepseek-flash', label: 'DEEPSEEK-V4.1-FLASH' },
    { id: 'deepseek-v4-pro', label: 'DEEPSEEK-V4-PRO' },
  ],
  qwen: [
    { id: 'qwen-max', label: 'QWEN-MAX' },
    { id: 'qwen-plus', label: 'QWEN-PLUS' },
  ],
  doubao: [
    { id: 'doubao-pro-32k', label: 'DOUBAO-PRO-32K' },
    { id: 'doubao-lite-4k', label: 'DOUBAO-LITE-4K' },
  ],
  // 智谱：文本走 chat，图像/视频模型走 /api/media 生成接口（均为官方免费模型）
  zhipu: [
    { id: 'glm-4.7-flash', label: 'GLM-4.7-FLASH' },
    { id: 'cogview-3-flash', label: 'COGVIEW-3-FLASH · 文生图' },
    { id: 'cogvideox-flash', label: 'COGVIDEOX-FLASH · 文生/图生视频' },
  ],
}

/**
 * 生成类模型（图像/视频）：这些模型**不能对话**，选中后走媒体生成流程。
 *
 * 判定集中在此，避免 ChatView 直接硬编码模型名。
 */
export const MEDIA_MODELS: Record<string, 'image' | 'video'> = {
  'cogview-3-flash': 'image',
  'cogvideox-flash': 'video',
}

export function mediaKindOf(model: string): 'image' | 'video' | null {
  return MEDIA_MODELS[model] ?? null
}

/**
 * 取某 provider 应展示的模型选项。
 *
 * @param providerId provider id（如 deepseek / qwen / custom_xxx）
 * @param availableIds 该 provider 配置的可用模型 id 列表
 * @returns 选项列表；provider 无可用模型时返回空数组
 */
export function modelsForProvider(providerId: string, availableIds: string[]): CatalogModel[] {
  if (!availableIds.length) return []
  const curated = MODEL_CATALOG[providerId]
  if (curated) {
    const matched = curated.filter((m) => availableIds.includes(m.id))
    if (matched.length) return matched
  }
  return availableIds.map((id) => ({ id, label: id.toUpperCase() }))
}
