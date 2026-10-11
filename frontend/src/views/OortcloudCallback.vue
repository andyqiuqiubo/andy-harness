<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiClient } from '../api/client'
import { useProviderStore } from '../stores/providers'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()
const router = useRouter()
const providerStore = useProviderStore()

const status = ref<'loading' | 'success' | 'error'>('loading')
const message = ref('')
const busy = ref(false)

/** 登录页可能用 query（?a=b）或 hash（#a=b）回传参数，两种都兼容。 */
function parseCallbackParams(): URLSearchParams {
  const q = window.location.search
  if (q && q.length > 1) return new URLSearchParams(q.slice(1))
  const h = window.location.hash
  if (h && h.includes('=')) {
    const raw = h.replace(/^#/, '').replace(/^\?/, '')
    return new URLSearchParams(raw)
  }
  return new URLSearchParams()
}

async function handleCallback() {
  const params = parseCallbackParams()
  const accessToken = params.get('access_token') || params.get('accessToken') || ''
  const tenantId = params.get('tenant_id') || params.get('tenantId') || ''
  const state = params.get('state') || ''
  const returnTo = decodeURIComponent(params.get('returnTo') || '/settings?tab=providers')

  if (!accessToken) {
    status.value = 'error'
    message.value = t.value('oortcloud.callbackNoToken')
    return
  }

  busy.value = true
  try {
    const res = await apiClient.post<{ ok: boolean; detail?: string; error?: string }>(
      '/integrations/oortcloud/connect',
      { mode: 'oauth', credentials: { access_token: accessToken, tenant_id: tenantId, state } },
    )
    if (res.ok) {
      status.value = 'success'
      message.value = res.detail || t.value('oortcloud.callbackSuccess')
      // 刷新 Provider 列表，让新建的 custom_oortcloud 立即出现在各页面
      try {
        await providerStore.loadProviders()
      } catch {
        // 刷新失败不影响跳转
      }
      // 稍作停留展示成功提示，再跳回原页面
      setTimeout(() => router.replace(returnTo), 1200)
    } else {
      status.value = 'error'
      message.value = res.error || t.value('oortcloud.callbackFail')
    }
  } catch (e) {
    status.value = 'error'
    message.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

onMounted(handleCallback)
</script>

<template>
  <div class="oort-callback">
    <div class="oort-callback-card card">
      <div v-if="status === 'loading'" class="oort-status oort-loading">
        <svg class="oort-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="2" x2="12" y2="6" /><line x1="12" y1="18" x2="12" y2="22" />
          <line x1="4.93" y1="4.93" x2="7.76" y2="7.76" /><line x1="16.24" y1="16.24" x2="19.07" y2="19.07" />
          <line x1="2" y1="12" x2="6" y2="12" /><line x1="18" y1="12" x2="22" y2="12" />
          <line x1="4.93" y1="19.07" x2="7.76" y2="16.24" /><line x1="16.24" y1="7.76" x2="19.07" y2="4.93" />
        </svg>
        <p>{{ t('oortcloud.callbackProcessing') }}</p>
      </div>

      <div v-else-if="status === 'success'" class="oort-status oort-ok">
        <svg class="oort-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
        <p>{{ message }}</p>
        <p class="oort-sub">{{ t('oortcloud.callbackRedirecting') }}</p>
      </div>

      <div v-else class="oort-status oort-err">
        <svg class="oort-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
        <p>{{ message }}</p>
        <button class="btn-ghost btn-sm" @click="router.replace('/settings?tab=providers')">
          {{ t('oortcloud.callbackBack') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.oort-callback {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
  padding: var(--space-xl);
}
.oort-callback-card {
  max-width: 420px;
  width: 100%;
  padding: var(--space-xl);
  text-align: center;
}
.oort-status {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-sm);
  color: var(--color-text);
}
.oort-status p {
  margin: 0;
  font-size: 0.95rem;
}
.oort-sub {
  color: var(--color-text-tertiary);
  font-size: 0.82rem !important;
}
.oort-icon {
  width: 42px;
  height: 42px;
}
.oort-ok .oort-icon {
  color: var(--color-success, #2e9e5b);
}
.oort-err .oort-icon {
  color: var(--color-danger);
}
.oort-spinner {
  width: 36px;
  height: 36px;
  animation: oort-spin 1s linear infinite;
  color: var(--color-primary);
}
.oort-loading p {
  color: var(--color-text-secondary);
}
@keyframes oort-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
