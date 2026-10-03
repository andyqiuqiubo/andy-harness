import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 用 vi.hoisted 保证 mock 在被提升的 vi.mock 工厂执行前已初始化。
const { getMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  deleteMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  apiClient: {
    get: (...args: unknown[]) => getMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
}))

import { useTodoStore } from './todos'

describe('useTodoStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    getMock.mockReset()
    deleteMock.mockReset()
  })

  it('loadTodos 成功时填充 todos / summary / available', async () => {
    getMock.mockResolvedValue({
      available: true,
      todos: [{ id: '1', content: 'a', status: 'pending', position: 0 }],
      summary: { pending: 1 },
    })
    const store = useTodoStore()
    await store.loadTodos('sess-1')

    expect(store.available).toBe(true)
    expect(store.todos).toHaveLength(1)
    expect(store.summary).toEqual({ pending: 1 })
    expect(getMock).toHaveBeenCalledWith('/sessions/sess-1/todos')
  })

  it('loadTodos 请求失败时安全清空', async () => {
    getMock.mockRejectedValue(new Error('network'))
    const store = useTodoStore()
    await store.loadTodos('sess-1')

    expect(store.available).toBe(false)
    expect(store.todos).toEqual([])
    expect(store.summary).toEqual({})
  })

  it('空 sessionId 时直接清空、不发请求', async () => {
    const store = useTodoStore()
    await store.loadTodos('')
    expect(getMock).not.toHaveBeenCalled()
    expect(store.todos).toEqual([])
  })

  it('clearTodos 调 delete 后重新加载', async () => {
    getMock.mockResolvedValue({
      available: false,
      todos: [],
      summary: {},
    })
    const store = useTodoStore()
    await store.clearTodos('sess-1')

    expect(deleteMock).toHaveBeenCalledWith('/sessions/sess-1/todos')
    expect(getMock).toHaveBeenCalled()
  })

  it('reset 清空状态', () => {
    const store = useTodoStore()
    store.todos = [{ id: '1', content: 'a', status: 'pending', position: 0 }]
    store.reset()
    expect(store.todos).toEqual([])
  })
})
