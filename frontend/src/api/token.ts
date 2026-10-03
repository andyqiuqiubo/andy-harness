/**
 * E12：认证 token 的本地持久化。
 *
 * 键名固定为 'harness_token'，仅存 token 字符串；用户信息由 auth store 管理。
 * 认证默认关闭时该键始终为空，不影响任何现有行为。
 */

const TOKEN_KEY = 'harness_token'

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // 隐私模式等场景下落空，调用方仍可在内存中持有 token。
  }
}

export function clearToken(): void {
  setToken('')
}
