export interface Session {
  id: string
  title: string
  config: Record<string, unknown>
  created_at: string
  updated_at: string
  archived: boolean
  message_count?: number
}

export interface Message {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  attachments?: Attachment[]
  tool_calls?: ToolCall[]
  tokens: number
  latency_ms?: number
  usage?: TokenUsage
  reasoning?: string
  created_at: string
}

/** 附件公开元信息（不含存储路径，与后端 AttachmentRepository.create 对齐）。 */
export interface Attachment {
  id: string
  kind: 'document' | 'image'
  filename: string
  mime: string
  size: number
}

/**
 * 执行过程步骤（回答生成过程中的思维链 / 中间说明 / 工具调用）。
 * - 实时：stores/chat.processEvents 由 WS token_delta / tool_event 累积
 * - 历史：ChatView 把中间 assistant 消息（带 tool_calls）归并为该结构
 * 工具字段与 WS tool_event 及历史消息转换后的 tool_calls 结构一致
 * （tool_name / args / result / error）。
 */
export interface ProcessStep {
  kind: 'reasoning' | 'text' | 'tool'
  /** kind=reasoning|text 时的文本 */
  text?: string
  /** kind=tool 时的工具调用信息 */
  tool?: {
    tool_name?: string
    args?: Record<string, unknown>
    result?: string
    error?: string
  }
}

export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  prompt_tokens_details?: {
    cached_tokens?: number
    prompt_cache_hit_tokens?: number
    prompt_cache_miss_tokens?: number
  }
}

export interface ToolCall {
  tool_name: string
  args: Record<string, unknown>
  result: string
  error?: string | null
}

export interface Provider {
  id: string
  name: string
  base_url: string
  models: string[]
  has_api_key: boolean
  enabled: boolean
}

export interface Model {
  id: string
  name: string
  provider: string
  provider_name: string
}

export interface WSFrame {
  type:
    | 'token_delta'
    | 'tool_event'
    | 'context_snapshot'
    | 'error'
    | 'done'
    | 'stop_ack'
    | 'confirm_request'
    | 'confirm_timeout'
    /** 服务端告知附件未被本次提问采纳（P1-1，不阻断对话） */
    | 'attachment_warning'
  data: Record<string, unknown>
}

/** 权限策略模式 */
export type PermissionMode = 'auto' | 'confirm_dangerous' | 'confirm_write' | 'confirm_all'

/** 单工具权限覆盖动作 */
export type PermissionAction = 'auto' | 'confirm' | 'deny'

export interface ToolPermission {
  name: string
  risk: 'read' | 'write' | 'dangerous'
  action: 'allow' | 'confirm' | 'deny'
  reason: string
  owner: string
}

export interface PermissionsState {
  available: boolean
  mode: PermissionMode | null
  modes: PermissionMode[]
  actions: PermissionAction[]
  overrides: Record<string, PermissionAction>
  tools: ToolPermission[]
}
