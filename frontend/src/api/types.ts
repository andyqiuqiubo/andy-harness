export interface Session {
  id: string
  title: string
  config: Record<string, unknown>
  created_at: string
  updated_at: string
  archived: boolean
}

export interface Message {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  tool_calls?: ToolCall[]
  tokens: number
  latency_ms?: number
  created_at: string
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
  type: 'token_delta' | 'tool_event' | 'context_snapshot' | 'error' | 'done'
  data: Record<string, unknown>
}
