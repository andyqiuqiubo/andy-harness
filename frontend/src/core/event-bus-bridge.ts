type EventHandler = (data: unknown) => void

export class EventBusBridgeImpl {
  private handlers: Map<string, Set<EventHandler>> = new Map()

  on(event: string, handler: EventHandler): void {
    if (!this.handlers.has(event)) {
      this.handlers.set(event, new Set())
    }
    this.handlers.get(event)!.add(handler)
  }

  off(event: string, handler: EventHandler): void {
    this.handlers.get(event)?.delete(handler)
  }

  emit(event: string, data: unknown): void {
    this.handlers.get(event)?.forEach((h) => h(data))
  }

  clear(): void {
    this.handlers.clear()
  }
}

export const eventBus = new EventBusBridgeImpl()
