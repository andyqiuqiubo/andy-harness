<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiClient } from '../api/client'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

interface Tpl {
  key: string
  type: string
  label: string
}
interface PluginRow {
  id: string
  name: string
  version?: string
  type?: string
  activated?: boolean
  core?: boolean
  config_schema?: {
    properties?: Record<string, { type?: string; description?: string; default?: unknown; enum?: unknown[] }>
  } | null
}
interface ConfigState {
  schema: {
    properties?: Record<string, { type?: string; description?: string; enum?: unknown[] }>
  } | null
  config: Record<string, unknown>
}

const templates = ref<Tpl[]>([])
const plugins = ref<PluginRow[]>([])
const loadError = ref('')
const showGuide = ref(true)

// 脚手架表单
const tplKey = ref('tool')
const newId = ref('')
const newName = ref('')
const newDesc = ref('')
const scaffoldResult = ref('')
const scaffoldError = ref('')
const scaffolding = ref(false)

// 配置面板
const configPluginId = ref('')
const configState = ref<ConfigState | null>(null)
const configSaved = ref(false)

// 重载
const reloading = ref('')
const reloadMsg = ref('')

const configProps = computed(() => Object.entries(configState.value?.schema?.properties || {}))

async function loadPlugins() {
  try {
    const res = await apiClient.get<{ plugins?: PluginRow[] } | PluginRow[]>('/plugins')
    plugins.value = Array.isArray(res) ? res : (res.plugins || [])
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}

async function loadTemplates() {
  try {
    templates.value = await apiClient.get<Tpl[]>('/plugins/dev/templates')
  } catch {
    templates.value = []
  }
}

async function doScaffold() {
  scaffoldError.value = ''
  scaffoldResult.value = ''
  scaffolding.value = true
  try {
    const res = await apiClient.post<{ dir: string; files: string[]; hint: string }>(
      '/plugins/dev/scaffold',
      { template: tplKey.value, id: newId.value, name: newName.value, description: newDesc.value },
    )
    scaffoldResult.value = `${t.value('devkit.scaffoldDone')}：${res.dir}（${res.files.join(', ')}）`
    newId.value = ''
    newName.value = ''
    newDesc.value = ''
    await loadPlugins()
  } catch (e) {
    scaffoldError.value = e instanceof Error ? e.message : String(e)
  } finally {
    scaffolding.value = false
  }
}

async function reloadPlugin(id: string) {
  reloading.value = id
  reloadMsg.value = ''
  try {
    await apiClient.post(`/plugins/dev/reload/${id}`, {})
    reloadMsg.value = t.value('devkit.reloadOk').replace('{0}', id)
    await loadPlugins()
  } catch (e) {
    reloadMsg.value = t.value('devkit.reloadFail').replace('{0}', id) + (e instanceof Error ? `：${e.message}` : '')
  } finally {
    reloading.value = ''
  }
}

async function openConfig(id: string) {
  configSaved.value = false
  configPluginId.value = id
  try {
    configState.value = await apiClient.get<ConfigState>(`/plugins/${id}/config`)
  } catch {
    configState.value = null
  }
}

async function saveConfig() {
  if (!configPluginId.value || !configState.value) return
  try {
    await apiClient.patch(`/plugins/${configPluginId.value}/config`, {
      config: configState.value.config,
    })
    configSaved.value = true
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(async () => {
  await loadTemplates()
  await loadPlugins()
  const first = plugins.value.find((p) => p.config_schema)
  if (first) await openConfig(first.id)
})
</script>

<template>
  <div class="dv-view">
    <header class="dv-header">
      <router-link to="/chat" class="dv-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('insights.back') }}
      </router-link>
      <h2 class="dv-title">{{ t('devkit.title') }}</h2>
      <p class="dv-sub">{{ t('devkit.subtitle') }}</p>
    </header>

    <div v-if="loadError" class="dv-error">{{ loadError }}</div>

    <!-- 使用示例：5 分钟跑通第一个插件 -->
    <section class="card dv-guide">
      <div class="section-head">
        <h3 class="section-title">📖 {{ t('devkit.guideTitle') }}</h3>
        <button class="btn-ghost btn-xs" @click="showGuide = !showGuide">
          {{ showGuide ? t('evals.hideCases') : t('evals.viewCases') }}
        </button>
      </div>
      <ol v-if="showGuide" class="guide-steps">
        <li>{{ t('devkit.guideStep1') }}</li>
        <li>{{ t('devkit.guideStep2') }}</li>
        <li>{{ t('devkit.guideStep3') }}</li>
        <li>{{ t('devkit.guideStep4') }}</li>
        <li>{{ t('devkit.guideStep5') }}</li>
      </ol>
    </section>

    <!-- 脚手架 -->
    <section class="card dv-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('devkit.scaffold') }}</h3>
      </div>
      <div class="tpl-cards">
        <button
          v-for="tp in templates"
          :key="tp.key"
          :class="['tpl-card', { active: tplKey === tp.key }]"
          @click="tplKey = tp.key"
        >
          <strong>{{ tp.label }}</strong>
          <span class="tpl-type">{{ tp.type }}</span>
        </button>
      </div>
      <div class="scaffold-form">
        <input v-model="newId" class="form-input" :placeholder="t('devkit.idPh')" />
        <input v-model="newName" class="form-input" :placeholder="t('templates.namePh')" />
        <input v-model="newDesc" class="form-input" :placeholder="t('templates.descPh')" />
        <button class="btn-primary btn-sm" :disabled="!newId || !newName || scaffolding" @click="doScaffold">
          {{ scaffolding ? t('insights.loading') : t('devkit.scaffoldBtn') }}
        </button>
      </div>
      <p v-if="scaffoldResult" class="dv-ok">{{ scaffoldResult }}</p>
      <p v-if="scaffoldError" class="dv-error">{{ scaffoldError }}</p>
    </section>

    <!-- 插件列表 + 重载 -->
    <section class="card dv-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('devkit.plugins') }}</h3>
        <span v-if="reloadMsg" class="dv-ok">{{ reloadMsg }}</span>
      </div>
      <div class="plugins-table-wrap">
        <table class="plugins-table">
          <thead>
            <tr>
              <th>{{ t('devkit.pluginName') }}</th>
              <th>id</th>
              <th>{{ t('devkit.state') }}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in plugins" :key="p.id">
              <td>{{ p.name }}</td>
              <td><code class="pid">{{ p.id }}</code></td>
              <td>
                <span :class="['run-status', p.activated ? 'st-done' : 'st-pending']">
                  {{ p.activated ? t('providers.enabled') : t('providers.disabled') }}
                </span>
              </td>
              <td class="actions">
                <button
                  class="btn-ghost btn-xs"
                  :disabled="!!reloading || p.core"
                  :title="p.core ? t('devkit.coreNoReload') : t('devkit.reload')"
                  @click="reloadPlugin(p.id)"
                >
                  {{ reloading === p.id ? t('insights.loading') : t('devkit.reload') }}
                </button>
                <button
                  v-if="p.config_schema"
                  class="btn-ghost btn-xs"
                  @click="openConfig(p.id)"
                >
                  {{ t('devkit.config') }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 配置面板 -->
    <section v-if="configState && configPluginId" class="card dv-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('devkit.configOf').replace('{0}', configPluginId) }}</h3>
        <span v-if="configSaved" class="dv-ok">{{ t('devkit.configSaved') }}</span>
      </div>
      <div v-if="configProps.length" class="config-grid">
        <div v-for="[key, prop] in configProps" :key="key" class="form-row">
          <label class="form-label" :title="prop.description">{{ key }}</label>
          <select
            v-if="Array.isArray(prop.enum)"
            v-model="(configState.config as Record<string, unknown>)[key]"
            class="form-input"
          >
            <option v-for="opt in prop.enum" :key="String(opt)" :value="opt">{{ opt }}</option>
          </select>
          <input
            v-else-if="prop.type === 'boolean'"
            type="checkbox"
            v-model="(configState.config as Record<string, unknown>)[key]"
          />
          <input
            v-else-if="prop.type === 'number' || prop.type === 'integer'"
            type="number"
            class="form-input"
            v-model.number="(configState.config as Record<string, unknown>)[key]"
          />
          <input
            v-else
            class="form-input"
            v-model="(configState.config as Record<string, unknown>)[key]"
          />
        </div>
      </div>
      <p v-else class="empty-hint">{{ t('devkit.noSchema') }}</p>
      <button v-if="configProps.length" class="btn-primary btn-sm" @click="saveConfig">
        {{ t('devkit.saveConfig') }}
      </button>
    </section>
  </div>
</template>

<style scoped>
.dv-view { min-height: 100vh; background: var(--bg-main); padding: var(--space-lg); max-width: 1080px; margin: 0 auto; }
.dv-header { display: flex; align-items: baseline; gap: var(--space-md); margin-bottom: var(--space-lg); flex-wrap: wrap; }
.dv-back { display: inline-flex; align-items: center; gap: 4px; font-size: var(--font-size-sm); color: var(--color-text-secondary); text-decoration: none; }
.dv-back:hover { color: var(--color-primary); }
.dv-title { margin: 0; font-size: var(--font-size-lg); font-weight: 700; color: var(--color-text); }
.dv-sub { margin: 0; font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.dv-section { padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-lg); }
/* 使用示例卡 */
.dv-guide { padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-lg); border-left: 3px solid var(--color-primary); }
.guide-steps { margin: var(--space-sm) 0 0; padding-left: var(--space-lg); color: var(--color-text); font-size: var(--font-size-sm); line-height: 1.9; }
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--space-sm); gap: var(--space-sm); flex-wrap: wrap; }
.section-title { margin: 0; font-size: var(--font-size-md); font-weight: 700; color: var(--color-text); }
.tpl-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: var(--space-sm); margin-bottom: var(--space-md); }
.tpl-card {
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  padding: 10px 14px; border: 1px solid var(--border-color); border-radius: var(--radius-md);
  background: var(--bg-input); color: var(--color-text); cursor: pointer; font-size: var(--font-size-sm);
  transition: var(--transition-base);
}
.tpl-card.active { border-color: var(--color-primary); background: var(--color-primary-light); color: var(--color-primary); }
.tpl-type { font-size: 11px; color: var(--color-text-tertiary); }
.scaffold-form { display: flex; gap: var(--space-sm); flex-wrap: wrap; }
.scaffold-form .form-input { flex: 1; min-width: 180px; }
.plugins-table-wrap { max-height: 380px; overflow-y: auto; }
.plugins-table { width: 100%; border-collapse: collapse; font-size: var(--font-size-sm); }
.plugins-table th { text-align: left; font-size: var(--font-size-xs); color: var(--color-text-tertiary); padding: 6px 8px; border-bottom: 1px solid var(--border-light); }
.plugins-table td { padding: 7px 8px; border-bottom: 1px solid var(--border-light); color: var(--color-text); }
.plugins-table .actions { text-align: right; white-space: nowrap; }
.pid { font-family: var(--font-mono, monospace); font-size: var(--font-size-xs); background: var(--bg-tag); padding: 2px 6px; border-radius: var(--radius-sm); }
.run-status { font-weight: 600; font-size: var(--font-size-xs); padding: 2px 8px; border-radius: var(--radius-full); }
.st-done { color: var(--color-success, #16a34a); background: var(--color-success-light); }
.st-pending { color: var(--color-text-tertiary); background: var(--bg-tag); }
.config-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: var(--space-sm); margin-bottom: var(--space-md); }
.dv-ok { color: var(--color-success, #16a34a); font-size: var(--font-size-xs); }
.dv-error { color: var(--color-danger); font-size: var(--font-size-xs); margin: var(--space-xs) 0; }
.empty-hint { color: var(--color-text-tertiary); font-size: var(--font-size-sm); padding: var(--space-sm) 0; }
</style>
