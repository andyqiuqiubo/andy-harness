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
  tool_calls?: ToolCall[]
  tokens: number
  latency_ms?: number
  usage?: TokenUsage
  created_at: string
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
