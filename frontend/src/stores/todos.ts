import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '../api/client'

export type TodoStatus = 'pending' | 'in_progress' | 'completed'

export interface TodoItem {
  id: string
  content: string
  status: TodoStatus
  position: number
}

interface TodoResponse {
  available: boolean
  todos: TodoItem[]
  summary: Record<string, number>
}

export const useTodoStore = defineStore('todos', () => {
  const todos = ref<TodoItem[]>([])
  const summary = ref<Record<string, number>>({})
  const available = ref(false)
  const loading = ref(false)

  async function loadTodos(sessionId: string) {
    if (!sessionId) {
      todos.value = []
      summary.value = {}
      return
    }
    loading.value = true
    try {
      const res = await apiClient.get<TodoResponse>(`/sessions/${sessionId}/todos`)
      available.value = res.available === true
      todos.value = res.todos ?? []
      summary.value = res.summary ?? {}
    } catch {
      todos.value = []
      summary.value = {}
      available.value = false
    } finally {
      loading.value = false
    }
  }

  async function clearTodos(sessionId: string) {
    if (!sessionId) return
    try {
      await apiClient.delete(`/sessions/${sessionId}/todos`)
      await loadTodos(sessionId)
    } catch {
      // 忽略：清空失败不影响主流程
    }
  }

  function reset() {
    todos.value = []
    summary.value = {}
  }

  return { todos, summary, available, loading, loadTodos, clearTodos, reset }
})
