<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useLanguage } from '../composables/useLanguage'

const authStore = useAuthStore()
const { t } = useLanguage()

const username = ref('')
const password = ref('')

async function onSubmit(): Promise<void> {
  if (authStore.loading) return
  await authStore.login(username.value.trim(), password.value)
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="onSubmit">
      <h1 class="login-title">{{ t('auth.title') }}</h1>
      <p class="login-subtitle">{{ t('auth.subtitle') }}</p>

      <label class="field">
        <span>{{ t('auth.username') }}</span>
        <input
          v-model="username"
          type="text"
          name="username"
          autocomplete="username"
          required
          autofocus
        />
      </label>

      <label class="field">
        <span>{{ t('auth.password') }}</span>
        <input
          v-model="password"
          type="password"
          name="password"
          autocomplete="current-password"
          required
        />
      </label>

      <p v-if="authStore.error" class="login-error">{{ authStore.error }}</p>

      <button class="login-btn" type="submit" :disabled="authStore.loading">
        {{ authStore.loading ? t('auth.submitting') : t('auth.submit') }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  width: 100%;
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg-primary, #0a0e14);
}

.login-card {
  width: 360px;
  max-width: calc(100vw - 48px);
  padding: 36px 32px;
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.1));
  border-radius: 14px;
  background: var(--bg-secondary, #12161f);
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.45);
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.login-title {
  margin: 0;
  font-size: 24px;
  color: var(--text-primary, #e8ecf1);
}

.login-subtitle {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--text-secondary, #9aa4b2);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-secondary, #9aa4b2);
}

.field input {
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.12));
  background: var(--bg-input, #0d1117);
  color: var(--text-primary, #e8ecf1);
  font-size: 14px;
  outline: none;
}

.field input:focus {
  border-color: var(--accent, #4f46e5);
}

.login-error {
  margin: 0;
  font-size: 13px;
  color: #ef4444;
}

.login-btn {
  margin-top: 6px;
  padding: 11px 14px;
  border: none;
  border-radius: 8px;
  background: var(--accent, #4f46e5);
  color: #fff;
  font-size: 14px;
  cursor: pointer;
  transition: opacity 0.15s ease;
}

.login-btn:hover {
  opacity: 0.92;
}

.login-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
