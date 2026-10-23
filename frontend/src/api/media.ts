import { apiClient } from './client'

/** 媒体生成任务（图像 / 视频）。后端为异步任务制，前端提交后轮询。 */
export interface MediaTask {
  id: string
  session_id: string
  kind: 'image' | 'video'
  model: string
  provider_id: string
  prompt: string
  params: Record<string, unknown>
  upstream_id: string
  /**
   * SUBMITTING（正在提交智谱）| PROCESSING（排队/生成中）| SUCCESS | FAIL
   * | CANCELLED（用户点「停止生成」，后端断开轮询；智谱无取消 API）
   */
  status: string
  error: string
  has_file: boolean
  mime: string
  size_bytes: number
  /** 已等待秒数（服务端计算，刷新后恢复也准确） */
  waited_seconds?: number
  /** 距最大等待截止的剩余秒数（0 = 已超时） */
  remaining_seconds?: number
  created_at: string
  updated_at: string
}

export interface MediaGenerationRequest {
  kind: 'image' | 'video'
  model: string
  prompt: string
  image?: string
  size?: string
  fps?: number
  quality?: 'speed' | 'quality'
  with_audio?: boolean
  watermark?: boolean
  session_id?: string
  provider_id?: string
  attempts?: number
  /** 最长等待秒数（默认 3600 = 1 小时），超时后端判 FAIL 并停止轮询上游 */
  max_wait_seconds?: number
  /** 参考图片元信息（供刷新后恢复问答区缩略图） */
  attachments?: { id: string; filename: string; mime: string }[]
}

/** 提交生成任务。视频为异步（返回 PROCESSING），图像通常同步完成。 */
export function createGeneration(req: MediaGenerationRequest): Promise<MediaTask> {
  return apiClient.post<MediaTask>('/media/generations', req)
}

/**
 * 停止（取消）进行中的生成任务。
 * 后端把任务置 CANCELLED 并停止向智谱轮询；智谱异步任务无取消 API，
 * 上游可能仍在排队，但其产物不会被下载落盘。幂等（已终态再停止无变化）。
 */
export function stopMediaTask(taskId: string): Promise<MediaTask> {
  return apiClient.post<MediaTask>(`/media/tasks/${taskId}/stop`)
}

/** 查询任务状态；poll=true 时后端顺带向上游查询一次。 */
export function getMediaTask(taskId: string, poll = false): Promise<MediaTask> {
  return apiClient.get<MediaTask>(`/media/tasks/${taskId}${poll ? '?poll=1' : ''}`)
}

/** 列出某会话的历史生成任务（用于刷新 / 切会话后恢复产物卡片）。 */
export function listMediaTasks(sessionId: string): Promise<MediaTask[]> {
  return apiClient.get<MediaTask[]>(
    `/media/tasks?session_id=${encodeURIComponent(sessionId)}`,
  )
}

/** 产物下载地址（需带 token 时由 apiClient 统一注入）。 */
export function mediaContentUrl(taskId: string): string {
  return `/api/media/tasks/${taskId}/content`
}

export interface MediaSizes {
  image: string[]
  video: string[]
  fps: number[]
}

export function getMediaSizes(): Promise<MediaSizes> {
  return apiClient.get<MediaSizes>('/media/sizes')
}

/**
 * 等待任务完成（前端轮询）。
 *
 * 后端每次 poll=1 只向上游查一次，这里以固定间隔逼近，避免把请求打满。
 */
export async function waitForTask(
  taskId: string,
  opts: { intervalMs?: number; timeoutMs?: number; onTick?: (t: MediaTask) => void } = {},
): Promise<MediaTask> {
  const interval = opts.intervalMs ?? 5000
  const timeout = opts.timeoutMs ?? 15 * 60 * 1000
  const started = Date.now()
  let task = await getMediaTask(taskId)
  while (task.status === 'PROCESSING') {
    if (Date.now() - started > timeout) throw new Error('生成超时，请稍后重试')
    await new Promise((r) => setTimeout(r, interval))
    task = await getMediaTask(taskId, true)
    opts.onTick?.(task)
  }
  if (task.status === 'FAIL') throw new Error(task.error || '生成失败')
  return task
}
