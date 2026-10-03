<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

const canvas = ref<HTMLCanvasElement | null>(null)
let ctx: CanvasRenderingContext2D | null = null
let animationId: number | null = null
let columns: number[] = []
let columnSpeeds: number[] = []
const fontSize = 22
let columnCount = 0
let width = 0
let height = 0

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
  columnSpeeds = new Array(columnCount).fill(0).map(() => 0.12 + Math.random() * 0.2)
}

function draw() {
  if (!ctx || !canvas.value) return

  // 半透明黑覆盖，制造拖尾（不透明度高 → 拖尾短、1/0 更清晰）
  ctx.fillStyle = 'rgba(0, 0, 0, 0.17)'
  ctx.fillRect(0, 0, width, height)

  ctx.font = `bold ${fontSize}px "JetBrains Mono", "Consolas", monospace`
  ctx.textBaseline = 'top'

  for (let i = 0; i < columnCount; i++) {
    const x = i * fontSize
    const y = columns[i]

    // 头部字符（亮青白，轻辉光）
    ctx.shadowColor = 'rgba(0, 255, 200, 0.7)'
    ctx.shadowBlur = 6
    ctx.fillStyle = 'rgba(205, 255, 248, 1)'
    ctx.fillText(getRandomChar(), x, y)
    ctx.shadowBlur = 0

    // 尾部字符 — 绿色渐变拖尾
    for (let j = 1; j < 12; j++) {
      const tailY = y - j * fontSize
      if (tailY < 0) break
      const alpha = (1 - j / 12) * 0.9
      ctx.fillStyle = `rgba(0, 200, 60, ${alpha})`
      ctx.fillText(getRandomChar(), x, tailY)
    }

    // 下移（更慢，便于看清 1/0）
    columns[i] += columnSpeeds[i] * fontSize * 0.5

    if (columns[i] > height + 100) {
      columns[i] = Math.random() * -200
      columnSpeeds[i] = 0.12 + Math.random() * 0.2
    }
  }

  animationId = requestAnimationFrame(draw)
}

onMounted(() => {
  if (!canvas.value) return
  ctx = canvas.value.getContext('2d')
  if (!ctx) return
  resize()
  draw()
  window.addEventListener('resize', resize)
})

onUnmounted(() => {
  if (animationId !== null) cancelAnimationFrame(animationId)
  window.removeEventListener('resize', resize)
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
}
</style>
