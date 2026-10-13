import { apiClient } from '../api/client'

/**
 * OortCloud（OortCodex）免费额度领取 —— 前端统一入口。
 *
 * 数据流（Web 部署）：
 * 1. 本模块向 `/api/integrations/oortcloud/login-url` 请求后端组装好的登录页地址
 *    （后端用 env 的平台/网关/服务名拼装，绝不携带运营方 appid/secretkey）。
 * 2. 浏览器整体跳转到登录页；用户授权后，登录页重定向到本应用的
 *    `/oauth/oortcloud/callback?access_token=...&tenant_id=...&state=...&returnTo=...`。
 * 3. 回调页（OortcloudCallback.vue）把 token 回传给 `/api/integrations/oortcloud/connect`（mode=oauth），
 *    后端据此领取令牌、自动创建 custom_oortcloud Provider。
 *
 * `returnTo` 经由 redirect_uri 透传，登录页回跳时原样带回，供回调页在成功后跳回原页面
 * （如会话页 / 设置页 Providers 标签）。
 */

const PROVIDER = 'oortcloud'

/** 生成随机 state（防 CSRF，同标签页内发起/回调比对）。 */
function randomState(): string {
  const arr = new Uint32Array(2)
  crypto.getRandomValues(arr)
  return Array.from(arr)
    .map((n) => n.toString(36))
    .join('')
}

/** 向后端请求组装好的 OortCloud 统一登录页地址。 */
export async function fetchOortcloudLoginUrl(
  returnTo: string,
  state?: string,
): Promise<string> {
  const origin = window.location.origin
  const redirectUri = `${origin}/oauth/oortcloud/callback?returnTo=${encodeURIComponent(returnTo)}`
  const qs = new URLSearchParams({
    redirect_uri: redirectUri,
  })
  if (state) qs.set('state', state)
  const res = await apiClient.get<{ login_url: string }>(
    `/integrations/${PROVIDER}/login-url?${qs.toString()}`,
  )
  return res.login_url
}

/** 跳转浏览器到 OortCloud 登录页，授权后由统一回调页回传 token。 */
export async function startOortcloudLogin(returnTo: string): Promise<void> {
  const state = randomState()
  // 写入同标签页 sessionStorage，回调页比对（CSRF 防护）。
  sessionStorage.setItem('oortcloud_oauth_state', state)
  const url = await fetchOortcloudLoginUrl(returnTo, state)
  window.location.href = url
}
