import type { Directive } from 'vue'

/**
 * 涟漪效果指令 — 点击时产生水波纹扩散动画
 * 用法: v-ripple 或 v-ripple="'rgba(99,102,241,0.3)'"
 */
export const vRipple: Directive<HTMLElement, string | undefined> = {
  mounted(el, binding) {
    el.style.position = el.style.position || 'relative'
    el.style.overflow = 'hidden'

    const color = binding.value || 'rgba(255, 255, 255, 0.4)'

    el.addEventListener('click', (e: MouseEvent) => {
      const rect = el.getBoundingClientRect()
      const size = Math.max(rect.width, rect.height)
      const x = e.clientX - rect.left - size / 2
      const y = e.clientY - rect.top - size / 2

      const ripple = document.createElement('span')
      ripple.style.cssText = `
        position: absolute;
        width: ${size}px;
        height: ${size}px;
        left: ${x}px;
        top: ${y}px;
        border-radius: 50%;
        background: ${color};
        transform: scale(0);
        opacity: 0.6;
        pointer-events: none;
        transition: transform 600ms cubic-bezier(0.4,0,0.2,1), opacity 600ms ease-out;
        z-index: 0;
      `

      el.appendChild(ripple)

      requestAnimationFrame(() => {
        ripple.style.transform = 'scale(2.5)'
        ripple.style.opacity = '0'
      })

      setTimeout(() => ripple.remove(), 600)
    })
  },
}

/**
 * 长按检测组合式函数
 * 返回一个用于绑定的事件处理器对象
 */
export function useLongPress(callback: () => void, duration = 500) {
  let timer: ReturnType<typeof setTimeout> | null = null

  const start = () => {
    timer = setTimeout(callback, duration)
  }

  const cancel = () => {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  return {
    start,
    cancel,
  }
}
