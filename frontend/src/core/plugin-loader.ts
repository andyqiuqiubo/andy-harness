import type { UIPluginManifest, PluginMenuItem } from './plugin-types'
import type { Router } from 'vue-router'
import { createPluginContext } from './plugin-context'

// Use Vite's import.meta.glob to discover plugin manifests and components at build time
const manifestModules = import.meta.glob('/src/plugins/*/ui-plugin.json', { eager: true, import: 'default' }) as Record<string, UIPluginManifest>
const componentGlobs = import.meta.glob('/src/plugins/*/components/*.vue')

export interface LoadedPlugin {
  manifest: UIPluginManifest
  active: boolean
  component?: unknown
  menuItems: PluginMenuItem[]
}

export class PluginLoader {
  private router: Router
  private loadedPlugins = new Map<string, LoadedPlugin>()

  constructor(router: Router) {
    this.router = router
  }

  /** Scan for ui-plugin.json files using Vite's import.meta.glob */
  scan(): { manifest: UIPluginManifest; path: string }[] {
    const results: { manifest: UIPluginManifest; path: string }[] = []
    for (const [path, manifest] of Object.entries(manifestModules)) {
      results.push({ manifest, path })
    }
    return results
  }

  /** Validate that a manifest has all required fields */
  validate(manifest: UIPluginManifest): boolean {
    if (!manifest.id || typeof manifest.id !== 'string') return false
    if (!manifest.name || typeof manifest.name !== 'string') return false
    if (!manifest.version || typeof manifest.version !== 'string') return false
    if (manifest.type !== 'ui') return false
    if (!manifest.entry || typeof manifest.entry !== 'string') return false
    return true
  }

  /** Load a plugin: resolve its component and store it */
  async load(manifest: UIPluginManifest, manifestPath: string): Promise<LoadedPlugin> {
    // Derive plugin directory from manifest path (e.g. /src/plugins/hello/ui-plugin.json -> /src/plugins/hello)
    const pluginDir = manifestPath.substring(0, manifestPath.lastIndexOf('/'))

    let component: unknown = undefined

    if (manifest.contributes?.views) {
      for (const view of manifest.contributes.views) {
        const componentPath = `${pluginDir}/${view.component}`
        const loader = componentGlobs[componentPath]
        if (loader) {
          const module = await (loader as () => Promise<unknown>)()
          component = (module as { default: unknown }).default
        } else {
          console.warn(`[PluginLoader] Component not found: ${componentPath}`)
        }
      }
    }

    const menuItems = manifest.contributes?.menu_items ?? []

    const loaded: LoadedPlugin = {
      manifest,
      active: false,
      component,
      menuItems,
    }
    this.loadedPlugins.set(manifest.id, loaded)
    return loaded
  }

  /** Activate a plugin: add routes and register menu items */
  activate(pluginId: string): void {
    const plugin = this.loadedPlugins.get(pluginId)
    if (!plugin || plugin.active) return

    const { manifest, component } = plugin
    const pluginRouter = {
      addRoute: (path: string, comp: unknown, name: string) => {
        this.router.addRoute({ path, name, component: comp as never })
      },
      removeRoute: (name: string) => {
        this.router.removeRoute(name)
      },
    }

    // Create plugin context
    createPluginContext(pluginId, pluginRouter, {})

    if (manifest.contributes?.views && component) {
      for (const view of manifest.contributes.views) {
        this.router.addRoute({
          path: view.route,
          name: view.id,
          component: component as never,
        })
      }
    }

    plugin.active = true
    console.log(`[PluginLoader] Activated plugin: ${pluginId}`)
  }

  /** Deactivate a plugin: remove routes and unregister menu items */
  deactivate(pluginId: string): void {
    const plugin = this.loadedPlugins.get(pluginId)
    if (!plugin || !plugin.active) return

    const { manifest } = plugin
    if (manifest.contributes?.views) {
      for (const view of manifest.contributes.views) {
        this.router.removeRoute(view.id)
      }
    }

    plugin.active = false
    console.log(`[PluginLoader] Deactivated plugin: ${pluginId}`)
  }

  getLoadedPlugins(): LoadedPlugin[] {
    return Array.from(this.loadedPlugins.values())
  }

  getActivePlugins(): LoadedPlugin[] {
    return Array.from(this.loadedPlugins.values()).filter((p) => p.active)
  }

  /** Scan, validate, load, and activate all discovered plugins */
  async initAll(): Promise<void> {
    const discovered = this.scan()
    for (const { manifest, path } of discovered) {
      if (!this.validate(manifest)) {
        console.warn(`[PluginLoader] Invalid manifest for plugin: ${manifest.id}`)
        continue
      }
      await this.load(manifest, path)
      this.activate(manifest.id)
    }
    console.log(`[PluginLoader] Initialized ${this.loadedPlugins.size} plugins`)
  }
}

let instance: PluginLoader | null = null

export function initPluginLoader(router: Router): PluginLoader {
  instance = new PluginLoader(router)
  return instance
}

export function getPluginLoader(): PluginLoader | null {
  return instance
}
