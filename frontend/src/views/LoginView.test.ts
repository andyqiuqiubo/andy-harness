/**
 * E12：登录视图测试。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LoginView from '../views/LoginView.vue'
import { useAuthStore } from '../stores/auth'
import { clearToken } from '../api/token'

function mountLogin() {
  const pinia = createPinia()
  const wrapper = mount(LoginView, {
    global: {
      plugins: [pinia],
    },
  })
  return { wrapper, store: useAuthStore(pinia) }
}

describe('LoginView（E12）', () => {
  beforeEach(() => {
    clearToken()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    clearToken()
  })

  it('renders username and password fields', () => {
    const { wrapper } = mountLogin()
    expect(wrapper.find('input[name="username"]').exists()).toBe(true)
    expect(wrapper.find('input[name="password"]').exists()).toBe(true)
    expect(wrapper.find('button[type="submit"]').exists()).toBe(true)
  })

  it('submit with valid credentials stores token', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        token: 'tok123',
        expires_in_seconds: 7200,
        user: {
          id: 'user_1',
          username: 'admin',
          is_admin: true,
        },
      }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const { wrapper, store } = mountLogin()
    await wrapper.find('input[name="username"]').setValue('admin')
    await wrapper.find('input[name="password"]').setValue('secret123')
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(store.token).toBe('tok123')
    expect(store.user?.username).toBe('admin')
    expect(store.error).toBe('')
  })

  it('submit with bad credentials shows error', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 401,
      json: async () => ({
        code: 'INVALID_CREDENTIALS',
        message: '用户名或密码错误',
      }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const { wrapper, store } = mountLogin()
    await wrapper.find('input[name="username"]').setValue('admin')
    await wrapper.find('input[name="password"]').setValue('nope')
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(store.token).toBe('')
    expect(store.error).toBe('用户名或密码错误')
    expect(wrapper.find('.login-error').exists()).toBe(true)
  })
})
