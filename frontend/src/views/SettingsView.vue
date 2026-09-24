<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useProviderStore } from '../stores/providers'
import { usePluginStore, type PluginInfo, type InstallPluginRequest } from '../stores/plugins'
import { useSettingsStore, THEME_LIST, type Theme } from '../stores/settings'
import { useLanguage } from '../composables/useLanguage'
import { apiClient } from '../api/client'

type Tab = 'providers' | 'session' | 'plugins' | 'general'

const providerStore = useProviderStore()
const pluginStore = usePluginStore()
const settingsStore = useSettingsStore()
const { t } = useLanguage()

const activeTab = ref<Tab>('providers')

// Provider 表单
const showAddProvider = ref(false)
const newProvider = ref({
  name: '',
  base_url: '',
  api_key: '',
  models: '',
})
const testStatus = ref<Record<string, { loading: boolean; result: boolean | null }>>({})

// H10: Provider edit state
const editingProvider = ref<string | null>(null)
const editForm = ref({
  name: '',
  base_url: '',
  models: '',
  api_key: '',
})

// S26: Session settings bound to settings store
const sessionSettings = computed({
  get: () => settingsStore.sessionSettings,
  set: (val) => settingsStore.setSessionSettings(val),
})

// S27: Plugin config state
const selectedPluginForConfig = ref<string | null>(null)
const pluginConfigForm = ref<Record<string, unknown>>({})

// S26: Save session settings
function handleSaveSessionSettings() {
  settingsStore.setSessionSettings({
    model: sessionSettings.value.model,
    temperature: sessionSettings.value.temperature,
    system_prompt: sessionSettings.value.system_prompt,
  })
  alert(t.value('session.saved'))
}

async function handleAddProvider() {
  if (!newProvider.value.name || !newProvider.value.base_url || !newProvider.value.api_key) return
  try {
    await apiClient.post('/providers', {
      name: newProvider.value.name,
      base_url: newProvider.value.base_url,
      api_key: newProvider.value.api_key,
      models: newProvider.value.models ? newProvider.value.models.split(',').map((m) => m.trim()) : undefined,
    })
    await providerStore.loadProviders()
    showAddProvider.value = false
    newProvider.value = { name: '', base_url: '', api_key: '', models: '' }
  } catch (e) {
    alert('添加失败: ' + e)
  }
}

async function handleDeleteProvider(id: string) {
  if (!confirm('确认删除此 Provider?')) return
  try {
    await apiClient.delete(`/providers/${id}`)
    await providerStore.loadProviders()
  } catch (e) {
    alert('删除失败: ' + e)
  }
}

async function handleTestProvider(id: string) {
  testStatus.value[id] = { loading: true, result: null }
  try {
    const res = await apiClient.post<{ connected: boolean }>(`/providers/${id}/test`)
    testStatus.value[id] = { loading: false, result: res.connected }
  } catch {
    testStatus.value[id] = { loading: false, result: false }
  }
}

// H10: Start editing a provider
function startEditProvider(id: string) {
  const p = providerStore.providers.find((p) => p.id === id)
  if (!p) return
  editingProvider.value = id
  editForm.value = {
    name: p.name,
    base_url: p.base_url,
    models: p.models.join(', '),
    api_key: '',
  }
}

// H10: Save provider edit
async function handleSaveProvider(id: string) {
  try {
    await providerStore.updateProvider(id, {
      name: editForm.value.name,
      base_url: editForm.value.base_url,
      models: editForm.value.models ? editForm.value.models.split(',').map((m) => m.trim()) : [],
      ...(editForm.value.api_key ? { api_key: editForm.value.api_key } : {}),
    })
    editingProvider.value = null
  } catch (e) {
    alert('保存失败: ' + e)
  }
}

// H10: Toggle provider enabled/disabled
async function handleToggleProvider(id: string) {
  try {
    await providerStore.toggleProviderEnabled(id)
  } catch (e) {
    alert('操作失败: ' + e)
  }
}

async function handleActivatePlugin(id: string) {
  try {
    await pluginStore.activatePlugin(id)
  } catch (e) {
    alert('激活失败: ' + e)
  }
}

async function handleDeactivatePlugin(id: string) {
  try {
    await pluginStore.deactivatePlugin(id)
  } catch (e) {
    const msg = String(e)
    if (msg.includes('PLUGIN_DEACTIVATE_FORBIDDEN') || msg.includes('核心插件')) {
      alert('核心插件不可停用')
    } else {
      alert('停用失败: ' + e)
    }
  }
}

// S27: Load plugin config schema for rendering
async function handleLoadPluginConfig(plugin: PluginInfo) {
  selectedPluginForConfig.value = plugin.id
  const res = await pluginStore.fetchPluginConfig(plugin.id)
  if (res?.config) {
    pluginConfigForm.value = { ...res.config }
  } else {
    pluginConfigForm.value = {}
  }
}

// S27: Save plugin config
async function handleSavePluginConfig(id: string) {
  try {
    await pluginStore.savePluginConfig(id, pluginConfigForm.value)
    alert(t.value('plugins.saveConfig') + ' ✓')
  } catch (e) {
    alert('保存配置失败: ' + e)
  }
}

// S27: Get config schema properties for rendering
function getConfigSchemaProperties(plugin: PluginInfo): { key: string; type: string; description?: string; default?: unknown }[] {
  const schema = plugin.config_schema as { properties?: Record<string, unknown> } | undefined
  if (!schema?.properties) return []
  return Object.entries(schema.properties).map(([key, val]) => ({
    key,
    type: (val as { type?: string }).type || 'string',
    description: (val as { description?: string }).description,
    default: (val as { default?: unknown }).default,
  }))
}

// 插件安装表单
const showInstallForm = ref(false)
const installForm = ref({
  plugin_id: '',
  name: '',
  entry: '',
  description: '',
  plugin_code: '',
})

// 卸载插件
async function handleUninstallPlugin(id: string) {
  if (!confirm(`确定卸载插件 ${id}？此操作将删除插件文件，不可撤销。`)) return
  try {
    await pluginStore.uninstallPlugin(id)
  } catch (e) {
    alert('卸载失败: ' + e)
  }
}

// 安装插件
async function handleInstallPlugin() {
  if (!installForm.value.plugin_id || !installForm.value.name || !installForm.value.entry || !installForm.value.plugin_code) {
    alert('请填写所有必填字段')
    return
  }
  try {
    const req: InstallPluginRequest = {
      plugin_id: installForm.value.plugin_id,
      name: installForm.value.name,
      entry: installForm.value.entry,
      description: installForm.value.description,
      plugin_code: installForm.value.plugin_code,
      type: 'tool',
    }
    await pluginStore.installPlugin(req)
    showInstallForm.value = false
    installForm.value = { plugin_id: '', name: '', entry: '', description: '', plugin_code: '' }
  } catch (e) {
    alert('安装失败: ' + e)
  }
}

onMounted(() => {
  providerStore.loadProviders()
  pluginStore.loadPlugins()
})
</script>

<template>
  <div class="settings-view">
    <!-- Header with back link as ghost button -->
    <header class="settings-header">
      <router-link to="/chat" class="back-link">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        {{ t('settings.back') }}
      </router-link>
      <h1 class="settings-title">{{ t('settings.title') }}</h1>
    </header>

    <!-- Tab navigation as modern pill tabs -->
    <nav class="tab-nav">
      <button :class="['tab-btn', { active: activeTab === 'providers' }]" @click="activeTab = 'providers'">
        {{ t('settings.tab.providers') }}
      </button>
      <button :class="['tab-btn', { active: activeTab === 'session' }]" @click="activeTab = 'session'">
        {{ t('settings.tab.session') }}
      </button>
      <button :class="['tab-btn', { active: activeTab === 'plugins' }]" @click="activeTab = 'plugins'">
        {{ t('settings.tab.plugins') }}
      </button>
      <button :class="['tab-btn', { active: activeTab === 'general' }]" @click="activeTab = 'general'">
        {{ t('settings.tab.general') }}
      </button>
    </nav>

    <div class="settings-body">
      <!-- ═══════════════ Provider 设置 ═══════════════ -->
      <section v-if="activeTab === 'providers'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('providers.title') }}</h2>
          <button class="btn-primary" @click="showAddProvider = !showAddProvider">
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            {{ showAddProvider ? t('providers.cancel') : t('providers.add') }}
          </button>
        </div>

        <!-- 新增 Provider 表单 -->
        <Transition name="fade-slide">
          <div v-if="showAddProvider" class="add-form card">
            <h3 class="form-card-title">{{ t('providers.add') }}</h3>
            <div class="form-grid">
              <div class="form-row">
                <label class="form-label">{{ t('providers.name') }}</label>
                <input v-model="newProvider.name" class="form-input" placeholder="My Custom Model" />
              </div>
              <div class="form-row">
                <label class="form-label">{{ t('providers.baseUrl') }}</label>
                <input v-model="newProvider.base_url" class="form-input" placeholder="https://api.example.com/v1" />
              </div>
              <div class="form-row">
                <label class="form-label">{{ t('providers.apiKey') }}</label>
                <input v-model="newProvider.api_key" type="password" class="form-input" placeholder="sk-..." />
              </div>
              <div class="form-row">
                <label class="form-label">{{ t('providers.models') }}</label>
                <input v-model="newProvider.models" class="form-input" placeholder="gpt-4o, gpt-4o-mini" />
              </div>
            </div>
            <button class="btn-primary" @click="handleAddProvider">{{ t('providers.submit') }}</button>
          </div>
        </Transition>

        <!-- Provider 列表 -->
        <div class="provider-list">
          <div v-for="p in providerStore.providers" :key="p.id" class="provider-card card">
            <!-- Normal view -->
            <div v-if="editingProvider !== p.id" class="provider-row">
              <div class="provider-info">
                <div class="provider-name">
                  {{ p.name }}
                  <!-- M18: has_api_key indicator -->
                  <span v-if="p.has_api_key" class="badge-pill badge-ok">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                    {{ t('providers.hasApiKey') }}
                  </span>
                  <span v-else class="badge-pill badge-warn">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                    {{ t('providers.noApiKey') }}
                  </span>
                  <!-- H10: enabled/disabled status -->
                  <span :class="['badge-pill', p.enabled ? 'badge-enabled' : 'badge-disabled']">
                    {{ p.enabled ? t('providers.enabled') : t('providers.disabled') }}
                  </span>
                </div>
                <div class="provider-url">{{ p.base_url }}</div>
                <div class="provider-models">
                  <span v-for="m in p.models" :key="m" class="model-tag">{{ m }}</span>
                </div>
              </div>
              <div class="provider-actions">
                <button
                  class="btn-ghost"
                  @click="handleTestProvider(p.id)"
                  :disabled="testStatus[p.id]?.loading"
                >
                  <svg v-if="testStatus[p.id]?.loading" class="icon spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="4.93" y1="4.93" x2="7.76" y2="7.76" /><line x1="16.24" y1="16.24" x2="19.07" y2="19.07" /><line x1="2" y1="12" x2="6" y2="12" /><line x1="18" y1="12" x2="22" y2="12" /><line x1="4.93" y1="19.07" x2="7.76" y2="16.24" /><line x1="16.24" y1="7.76" x2="19.07" y2="4.93" /></svg>
                  <svg v-else class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                  {{ testStatus[p.id]?.loading ? t('providers.testing') : t('providers.test') }}
                </button>
                <span v-if="testStatus[p.id]?.result === true" class="test-result test-ok">
                  <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                  {{ t('providers.testOk') }}
                </span>
                <span v-if="testStatus[p.id]?.result === false" class="test-result test-fail">
                  <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                  {{ t('providers.testFail') }}
                </span>
                <!-- H10: Edit button -->
                <button class="btn-ghost" @click="startEditProvider(p.id)">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
                  {{ t('providers.edit') }}
                </button>
                <!-- H10: Enable/Disable toggle -->
                <button class="btn-ghost" @click="handleToggleProvider(p.id)">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0" /><line x1="12" y1="2" x2="12" y2="12" /></svg>
                  {{ p.enabled ? t('providers.disable') : t('providers.enable') }}
                </button>
                <button class="btn-ghost btn-danger" @click="handleDeleteProvider(p.id)">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                  {{ t('providers.delete') }}
                </button>
              </div>
            </div>

            <!-- Edit view -->
            <div v-else class="edit-form">
              <div class="form-grid">
                <div class="form-row">
                  <label class="form-label">{{ t('providers.name') }}</label>
                  <input v-model="editForm.name" class="form-input" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ t('providers.baseUrl') }}</label>
                  <input v-model="editForm.base_url" class="form-input" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ t('providers.models') }}</label>
                  <input v-model="editForm.models" class="form-input" placeholder="model1, model2" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ t('providers.apiKey') }} (留空不修改)</label>
                  <input v-model="editForm.api_key" type="password" class="form-input" placeholder="sk-..." />
                </div>
              </div>
              <div class="edit-actions">
                <button class="btn-primary" @click="handleSaveProvider(p.id)">{{ t('providers.edit') }}</button>
                <button class="btn-ghost" @click="editingProvider = null">{{ t('providers.cancel') }}</button>
              </div>
            </div>
          </div>
          <div v-if="providerStore.providers.length === 0" class="empty-hint">
            {{ t('providers.empty') }}
          </div>
        </div>
      </section>

      <!-- ═══════════════ 会话级设置 ═══════════════ -->
      <section v-if="activeTab === 'session'" class="tab-content">
        <h2 class="section-title">{{ t('session.title') }}</h2>
        <div class="card session-card">
          <div class="form-row">
            <label class="form-label">{{ t('session.model') }}</label>
            <input v-model="sessionSettings.model" class="form-input" placeholder="deepseek-chat" />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('session.temperature') }}: <span class="temp-value">{{ sessionSettings.temperature }}</span></label>
            <input
              v-model.number="sessionSettings.temperature"
              type="range"
              class="temp-slider"
              min="0"
              max="2"
              step="0.1"
            />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('session.systemPrompt') }}</label>
            <textarea v-model="sessionSettings.system_prompt" rows="4" class="form-input mono-input" placeholder="..."></textarea>
          </div>
          <!-- S26: Save button -->
          <button class="btn-primary" @click="handleSaveSessionSettings">{{ t('session.save') }}</button>
        </div>
      </section>

      <!-- ═══════════════ 插件管理 ═══════════════ -->
      <section v-if="activeTab === 'plugins'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('plugins.title') }}</h2>
          <button class="btn-primary" @click="showInstallForm = !showInstallForm">
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            {{ showInstallForm ? '取消' : '安装插件' }}
          </button>
        </div>

        <!-- 安装插件表单 -->
        <Transition name="fade-slide">
          <div v-if="showInstallForm" class="add-form card">
            <h3 class="form-card-title">安装新插件</h3>
            <div class="form-grid">
              <div class="form-row">
                <label class="form-label">插件 ID *</label>
                <input v-model="installForm.plugin_id" class="form-input" placeholder="tool_my_tool" />
              </div>
              <div class="form-row">
                <label class="form-label">插件名称 *</label>
                <input v-model="installForm.name" class="form-input" placeholder="My Tool" />
              </div>
              <div class="form-row">
                <label class="form-label">入口 (module:Class) *</label>
                <input v-model="installForm.entry" class="form-input" placeholder="plugins.tool_my_tool.main:MyToolPlugin" />
              </div>
              <div class="form-row">
                <label class="form-label">描述</label>
                <input v-model="installForm.description" class="form-input" placeholder="一个自定义工具插件" />
              </div>
            </div>
            <div class="form-row">
              <label class="form-label">插件 Python 源码 *</label>
              <textarea v-model="installForm.plugin_code" class="form-input code-textarea" rows="12" placeholder="from harness.kernel.contracts.tool import ToolPlugin&#10;..."></textarea>
            </div>
            <button class="btn-primary" @click="handleInstallPlugin">安装</button>
          </div>
        </Transition>

        <div class="plugin-list">
          <div v-for="p in pluginStore.plugins" :key="p.id" class="plugin-card card">
            <div class="plugin-row">
              <div class="plugin-info">
                <div class="plugin-name">
                  {{ p.name }}
                  <span v-if="p.activated" class="badge-pill badge-enabled">已启用</span>
                  <span v-else class="badge-pill badge-disabled">已停用</span>
                  <span v-if="p.core" class="badge-pill badge-core">{{ t('plugins.core') }}</span>
                </div>
                <div class="plugin-meta">
                  <span class="badge-pill badge-version">v{{ p.version }}</span>
                  <span class="badge-pill badge-type">{{ p.type }}</span>
                </div>
                <div v-if="p.permissions && p.permissions.length" class="plugin-permissions">
                  <span class="perm-label">权限:</span>
                  <span v-for="perm in p.permissions" :key="perm" class="perm-tag">{{ perm }}</span>
                </div>
              </div>
              <div class="plugin-actions">
                <label class="switch">
                  <input type="checkbox" :checked="p.activated" :disabled="p.core" @change="p.activated ? handleDeactivatePlugin(p.id) : handleActivatePlugin(p.id)" />
                  <span class="slider"></span>
                </label>
                <span v-if="p.core" class="core-lock" title="核心插件不可停用">
                  <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
                </span>
                <button class="btn-ghost" @click="handleLoadPluginConfig(p)" title="配置">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
                </button>
                <button v-if="!p.core" class="btn-ghost btn-uninstall" @click="handleUninstallPlugin(p.id)" title="卸载插件">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                </button>
              </div>
            </div>

            <Transition name="fade-slide">
              <div v-if="selectedPluginForConfig === p.id" class="plugin-config-form">
                <h4 class="config-title">{{ t('plugins.config') }}</h4>
                <div class="form-grid">
                  <div v-for="prop in getConfigSchemaProperties(p)" :key="prop.key" class="form-row">
                    <label class="form-label">{{ prop.key }}{{ prop.description ? ` (${prop.description})` : '' }}</label>
                    <input v-if="prop.type === 'string'" v-model="pluginConfigForm[prop.key as string]" class="form-input" :placeholder="String(prop.default ?? '')" />
                    <input v-else-if="prop.type === 'number'" type="number" class="form-input" v-model.number="pluginConfigForm[prop.key as string]" :placeholder="String(prop.default ?? '')" />
                    <input v-else-if="prop.type === 'boolean'" type="checkbox" v-model="pluginConfigForm[prop.key as string]" />
                    <input v-else v-model="pluginConfigForm[prop.key as string]" class="form-input" :placeholder="String(prop.default ?? '')" />
                  </div>
                </div>
                <button class="btn-primary" @click="handleSavePluginConfig(p.id)">{{ t('plugins.saveConfig') }}</button>
              </div>
            </Transition>
          </div>
          <div v-if="pluginStore.plugins.length === 0" class="empty-hint">{{ t('plugins.empty') }}</div>
        </div>
      </section>

      <!-- ═══════════════ 通用设置 ═══════════════ -->
      <section v-if="activeTab === 'general'" class="tab-content">
        <h2 class="section-title">{{ t('general.title') }}</h2>
        <div class="card general-card">
          <div class="form-row">
            <label class="form-label">{{ t('general.theme') }}</label>
            <div class="theme-grid">
              <button
                v-for="tm in THEME_LIST"
                :key="tm.id"
                :class="['theme-card', { active: settingsStore.theme === tm.id }]"
                @click="settingsStore.setTheme(tm.id as Theme)"
              >
                <span class="theme-swatch" :style="{ background: tm.color }"></span>
                <span class="theme-name">{{ tm.name }}</span>
                <svg v-if="settingsStore.theme === tm.id" class="theme-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </button>
            </div>
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('general.language') }}</label>
            <div class="pill-group">
              <button
                :class="['pill-btn', { active: settingsStore.language === 'zh' }]"
                @click="settingsStore.setLanguage('zh')"
              >中文</button>
              <button
                :class="['pill-btn', { active: settingsStore.language === 'en' }]"
                @click="settingsStore.setLanguage('en')"
              >English</button>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
/* ── Layout ────────────────────────────────────────────── */
.settings-view {
  max-width: 860px;
  margin: 0 auto;
  padding: var(--space-lg);
  animation: slideUp 0.3s ease;
}

/* ── Header ─────────────────────────────────────────────── */
.settings-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.back-link {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: var(--transition-base);
}

.back-link:hover {
  background: var(--bg-hover);
  color: var(--color-primary);
}

.back-link .icon {
  width: 18px;
  height: 18px;
}

.settings-title {
  font-size: var(--font-size-2xl);
  font-weight: 700;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

/* ── Tab Navigation ─────────────────────────────────────── */
.tab-nav {
  display: flex;
  gap: var(--space-xs);
  background: var(--bg-surface);
  padding: var(--space-xs);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  margin-bottom: var(--space-lg);
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.tab-btn {
  position: relative;
  padding: var(--space-sm) var(--space-lg);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  font-weight: 500;
  color: var(--color-text-secondary);
  transition: var(--transition-base);
  white-space: nowrap;
  z-index: 1;
}

.tab-btn::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 50%;
  width: 0;
  height: 3px;
  background: var(--color-primary);
  border-radius: var(--radius-full);
  transition: var(--transition-base);
  transform: translateX(-50%);
}

.tab-btn:hover {
  color: var(--color-text);
  background: var(--bg-hover);
}

.tab-btn.active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  font-weight: 600;
}

.tab-btn.active::after {
  width: 60%;
}

/* ── Tab Content ────────────────────────────────────────── */
.tab-content {
  animation: slideUp 0.25s ease;
}

/* ── Section Header ────────────────────────────────────── */
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-lg);
}

.section-title {
  font-size: var(--font-size-xl);
  font-weight: 700;
  color: var(--color-text);
}

/* ── Card Base ──────────────────────────────────────────── */
.card {
  background: var(--bg-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--border-light);
}

/* ── Provider / Plugin Lists ───────────────────────────── */
.provider-list,
.plugin-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
  max-height: 60vh;
  overflow-y: auto;
  padding-right: var(--space-xs);
}

/* 自定义滚动条 */
.provider-list::-webkit-scrollbar,
.plugin-list::-webkit-scrollbar {
  width: 6px;
}

.provider-list::-webkit-scrollbar-track,
.plugin-list::-webkit-scrollbar-track {
  background: var(--bg-main);
  border-radius: var(--radius-sm);
}

.provider-list::-webkit-scrollbar-thumb,
.plugin-list::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: var(--radius-sm);
}

.provider-list::-webkit-scrollbar-thumb:hover,
.plugin-list::-webkit-scrollbar-thumb:hover {
  background: var(--color-primary);
}

/* ── Provider Cards ────────────────────────────────────── */
.provider-card {
  padding: var(--space-lg);
  transition: var(--transition-base);
}

.provider-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

.provider-row,
.plugin-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-md);
}

.provider-info,
.plugin-info {
  flex: 1;
  min-width: 0;
}

.provider-name,
.plugin-name {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: var(--space-xs);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-xs);
}

.provider-url {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-sm);
  word-break: break-all;
}

.provider-models {
  display: flex;
  gap: var(--space-xs);
  flex-wrap: wrap;
}

/* ── Badge Pills ───────────────────────────────────────── */
.badge-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px var(--space-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  font-weight: 600;
  line-height: 1.5;
}

.badge-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.badge-warn {
  background: var(--color-warning-light);
  color: var(--color-warning);
}

.badge-enabled {
  background: var(--bg-success);
  color: var(--color-success);
}

.badge-disabled {
  background: var(--bg-disabled);
  color: var(--color-text-secondary);
}

.badge-core {
  background: var(--color-core-light);
  color: var(--color-core);
}

.badge-version {
  background: var(--bg-tag);
  color: var(--color-tag);
}

.badge-type {
  background: var(--bg-hover);
  color: var(--color-text-secondary);
}

.icon-sm {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

/* ── Model Tags ────────────────────────────────────────── */
.model-tag {
  background: var(--bg-tag);
  color: var(--color-tag);
  padding: 3px var(--space-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  font-weight: 500;
}

/* ── Provider / Plugin Actions ─────────────────────────── */
.provider-actions,
.plugin-actions {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
  flex-wrap: wrap;
}

/* ── Ghost Buttons ──────────────────────────────────────── */
.btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: var(--transition-base);
}

.btn-ghost:hover {
  background: var(--bg-hover);
  color: var(--color-text);
  border-color: var(--border-strong);
}

.btn-ghost:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-ghost.btn-danger:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.btn-ghost .icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

/* ── Primary Button ─────────────────────────────────────── */
.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-lg);
  border-radius: var(--radius-md);
  background: var(--color-primary);
  color: white;
  font-size: var(--font-size-sm);
  font-weight: 600;
  transition: var(--transition-base);
  box-shadow: var(--shadow-primary);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
  transform: translateY(-1px);
}

.btn-primary:active {
  transform: translateY(0);
}

.btn-primary .icon {
  width: 16px;
  height: 16px;
}

/* ── Test Result ───────────────────────────────────────── */
.test-result {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.test-ok {
  color: var(--color-success);
}

.test-fail {
  color: var(--color-danger);
}

/* ── Loading Spinner ───────────────────────────────────── */
.spin {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ── Add / Edit Form ────────────────────────────────────── */
.add-form,
.edit-form {
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}

.form-card-title {
  font-size: var(--font-size-md);
  font-weight: 600;
  margin-bottom: var(--space-md);
  color: var(--color-text);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-md);
}

.form-row {
  margin-bottom: var(--space-md);
}

.form-label {
  display: block;
  font-size: var(--font-size-sm);
  font-weight: 500;
  color: var(--color-text-secondary);
  margin-bottom: var(--space-xs);
}

.form-input {
  width: 100%;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  font-family: inherit;
  background: var(--bg-input);
  color: var(--color-text);
  transition: var(--transition-fast);
}

.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.form-input::placeholder {
  color: var(--color-text-tertiary);
}

.mono-input {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  resize: vertical;
}

.edit-actions {
  display: flex;
  gap: var(--space-sm);
}

/* ── Session Settings ──────────────────────────────────── */
.session-card {
  padding: var(--space-lg);
}

.temp-value {
  font-weight: 700;
  color: var(--color-primary);
}

.temp-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--bg-slider);
  outline: none;
  cursor: pointer;
}

.temp-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  transition: var(--transition-base);
  border: 3px solid var(--bg-surface);
}

.temp-slider::-webkit-slider-thumb:hover {
  transform: scale(1.2);
  box-shadow: var(--shadow-primary);
}

.temp-slider::-moz-range-thumb {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  border: 3px solid var(--bg-surface);
}

/* ── Plugin Cards ──────────────────────────────────────── */
.plugin-card {
  padding: var(--space-lg);
  transition: var(--transition-base);
}

.plugin-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

.plugin-meta {
  display: flex;
  gap: var(--space-xs);
  margin-bottom: var(--space-sm);
}

.plugin-permissions {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  flex-wrap: wrap;
  margin-top: var(--space-xs);
}

.perm-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  font-weight: 500;
}

.perm-tag {
  background: var(--bg-tag);
  color: var(--color-tag);
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  font-weight: 500;
}

.core-lock {
  color: var(--color-core);
  cursor: help;
  display: inline-flex;
  align-items: center;
}

/* ── Toggle Switch ─────────────────────────────────────── */
.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
  flex-shrink: 0;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  inset: 0;
  background: var(--bg-slider);
  transition: var(--transition-base);
  border-radius: var(--radius-full);
}

.slider::before {
  position: absolute;
  content: '';
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background: white;
  transition: var(--transition-bounce);
  border-radius: 50%;
  box-shadow: var(--shadow-sm);
}

.switch input:checked + .slider {
  background: var(--color-primary);
}

.switch input:checked + .slider::before {
  transform: translateX(20px);
}

.switch input:disabled + .slider {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Plugin Config Form ────────────────────────────────── */
.plugin-config-form {
  border-top: 1px solid var(--border-color);
  padding-top: var(--space-md);
  margin-top: var(--space-md);
}

.config-title {
  font-size: var(--font-size-sm);
  font-weight: 600;
  margin-bottom: var(--space-md);
  color: var(--color-text);
}

/* ── General Settings ──────────────────────────────────── */
.general-card {
  padding: var(--space-lg);
}

.pill-group {
  display: flex;
  gap: var(--space-sm);
}

/* ── Theme Grid ────────────────────────────────────────── */
.theme-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: var(--space-sm);
}

.theme-card {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-md) var(--space-sm);
  border-radius: var(--radius-md);
  border: 2px solid var(--border-color);
  background: var(--bg-input);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  cursor: pointer;
  transition: var(--transition-base);
}

.theme-card:hover {
  border-color: var(--border-strong);
  color: var(--color-text);
  transform: translateY(-2px);
}

.theme-card.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--bg-active);
  box-shadow: var(--shadow-primary);
}

.theme-swatch {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-full);
  box-shadow: 0 0 12px currentColor;
  flex-shrink: 0;
}

.theme-name {
  font-size: var(--font-size-xs);
}

.theme-check {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 14px;
  height: 14px;
  color: var(--color-primary);
}

.pill-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-md) var(--space-lg);
  border-radius: var(--radius-full);
  border: 2px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 600;
  transition: var(--transition-base);
}

.pill-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-text);
}

.pill-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: white;
  box-shadow: var(--shadow-primary);
}

.pill-btn .icon {
  width: 18px;
  height: 18px;
}

/* ── Empty Hint ────────────────────────────────────────── */
.empty-hint {
  padding: var(--space-xl) var(--space-md);
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: var(--font-size-sm);
  background: var(--bg-surface);
  border-radius: var(--radius-lg);
  border: 1px dashed var(--border-color);
}

/* ── Responsive ─────────────────────────────────────────── */
@media (max-width: 640px) {
  .settings-view {
    padding: var(--space-md);
  }

  .form-grid {
    grid-template-columns: 1fr;
  }

  .provider-row,
  .plugin-row {
    flex-direction: column;
  }

  .provider-actions,
  .plugin-actions {
    width: 100%;
  }

  .pill-group {
    flex-wrap: wrap;
  }
}

/* ── 插件安装 ── */
.code-textarea {
  font-family: var(--font-mono, 'JetBrains Mono', Consolas, monospace);
  font-size: var(--font-size-sm, 0.875rem);
  line-height: 1.6;
  resize: vertical;
  min-height: 200px;
}

.btn-uninstall {
  color: var(--color-danger, #ef4444);
}

.btn-uninstall:hover {
  background: var(--color-error-bg, rgba(239, 68, 68, 0.08));
}

.badge-enabled {
  background: var(--color-success-light, rgba(34, 197, 94, 0.1));
  color: var(--color-success, #22c55e);
}

.badge-disabled {
  background: var(--bg-disabled, #e2e8f0);
  color: var(--color-text-secondary, #64748b);
}
</style>
