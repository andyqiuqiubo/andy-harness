<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const canvas = ref<HTMLCanvasElement | null>(null)
let ctx: CanvasRenderingContext2D | null = null
let animationId: number | null = null
let columns: number[] = []
let columnSpeeds: number[] = []
let fontSize = 22
let columnCount = 0
let width = 0
let height = 0

// 鼠标位置（用于数字弯曲效果）
const mouse = { x: -9999, y: -9999, active: false }
const MOUSE_RADIUS = 250
const MOUSE_BEND_STRENGTH = 1.8

function getRandomChar(): string {
  return Math.random() < 0.5 ? '0' : '1'
}

function resize() {
  if (!canvas.value) return
  const dpr = window.devicePixelRatio || 1
  width = canvas.value.clientWidth
  height = canvas.value.clientHeight
  canvas.value.width = width * dpr
  canvas.value.height = height * dpr
  ctx!.scale(dpr, dpr)

  columnCount = Math.floor(width / fontSize)
  columns = new Array(columnCount).fill(0).map(() => Math.random() * -50)
  columnSpeeds = new Array(columnCount).fill(0).map(() => 0.4 + Math.random() * 0.4)
}

function draw() {
  if (!ctx || !canvas.value) return

  // 半透明黑色覆盖，制造拖尾效果
  ctx.fillStyle = 'rgba(0, 0, 0, 0.05)'
  ctx.fillRect(0, 0, width, height)

  ctx.font = `bold ${fontSize}px "JetBrains Mono", "Consolas", monospace`
  ctx.textBaseline = 'top'

  for (let i = 0; i < columnCount; i++) {
    const x = i * fontSize
    const y = columns[i]

    // 计算鼠标对当前列的弯曲影响
    let offsetX = 0
    let brightness = 1
    if (mouse.active) {
      const dx = x + fontSize / 2 - mouse.x
      const dy = y - mouse.y
      const dist = Math.sqrt(dx * dx + dy * dy)
      if (dist < MOUSE_RADIUS) {
        const influence = Math.pow(1 - dist / MOUSE_RADIUS, 1.5)
        offsetX = dx * influence * MOUSE_BEND_STRENGTH
        brightness = 1 + influence * 0.8
      }
    }

    // 头部字符 — 亮白色，加粗加发光
    const headChar = getRandomChar()
    ctx.shadowColor = 'rgba(0, 255, 100, 0.8)'
    ctx.shadowBlur = 8
    ctx.fillStyle = `rgba(240, 255, 240, ${Math.min(brightness, 1.8)})`
    ctx.fillText(headChar, x + offsetX, y)
    ctx.shadowBlur = 0

    // 尾部字符 — 绿色渐变
    for (let j = 1; j < 12; j++) {
      const tailY = y - j * fontSize
      if (tailY < 0) break
      let tailOffsetX = 0
      let tailBrightness = 1
      if (mouse.active) {
        const tdx = x + fontSize / 2 - mouse.x
        const tdy = tailY - mouse.y
        const tdist = Math.sqrt(tdx * tdx + tdy * tdy)
        if (tdist < MOUSE_RADIUS) {
          const tinf = Math.pow(1 - tdist / MOUSE_RADIUS, 1.5)
          tailOffsetX = tdx * tinf * MOUSE_BEND_STRENGTH
          tailBrightness = 1 + tinf * 0.8
        }
      }
      const alpha = (1 - j / 12) * 0.85
      const green = Math.floor(220 * tailBrightness)
      ctx.fillStyle = `rgba(0, ${Math.min(green, 255)}, ${Math.floor(green * 0.25)}, ${alpha})`
      ctx.fillText(getRandomChar(), x + tailOffsetX, tailY)
    }

    // 下移
    columns[i] += columnSpeeds[i] * fontSize * 0.5

    // 到底部后重置
    if (columns[i] > height + 100) {
      columns[i] = Math.random() * -200
      columnSpeeds[i] = 0.4 + Math.random() * 0.4
    }
  }

  animationId = requestAnimationFrame(draw)
}

function handleMouseMove(e: MouseEvent) {
  if (!canvas.value) return
  const rect = canvas.value.getBoundingClientRect()
  mouse.x = e.clientX - rect.left
  mouse.y = e.clientY - rect.top
  mouse.active = true
}

function handleMouseLeave() {
  mouse.active = false
  mouse.x = -9999
  mouse.y = -9999
}

onMounted(() => {
  if (!canvas.value) return
  ctx = canvas.value.getContext('2d')
  if (!ctx) return
  resize()
  draw()

  window.addEventListener('resize', resize)
  canvas.value.addEventListener('mousemove', handleMouseMove)
  canvas.value.addEventListener('mouseleave', handleMouseLeave)
})

onUnmounted(() => {
  if (animationId !== null) cancelAnimationFrame(animationId)
  window.removeEventListener('resize', resize)
  if (canvas.value) {
    canvas.value.removeEventListener('mousemove', handleMouseMove)
    canvas.value.removeEventListener('mouseleave', handleMouseLeave)
  }
})
</script>

<template>
  <canvas ref="canvas" class="matrix-rain"></canvas>
</template>

<style scoped>
.matrix-rain {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  cursor: crosshair;
}
</style>
