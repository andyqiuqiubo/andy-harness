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
/** 主画布与 sprite 共用的 devicePixelRatio（上限 2，见 resize） */
let dpr = Math.min(window.devicePixelRatio || 1, 2)

/* ── 性能：把「带辉光的字符」预渲染成 sprite，每帧只做 drawImage ──
 *
 * 旧实现每帧对每一列执行
 *   ctx.shadowBlur = 6; ctx.fillText(...)          // 头部字符（带辉光）
 *   for (j=1..11) ctx.fillText(...)                // 拖尾字符（11 次/列）
 * 1920 宽时有 87 列 → 每帧约 1000 次 fillText，其中 87 次还叠加 shadowBlur。
 * shadowBlur 是 Canvas 2D 里最昂贵的操作之一（需要独立光栅化 + 高斯模糊），
 * 这正是数字雨发卡的主因。
 *
 * 改法：字符只有 0 / 1 两种、样式只有「头部」和「11 档拖尾 alpha」，
 * 一共 2 + 11×2 = 24 张固定图形。启动时一次性画进离屏 canvas，
 * 运行时每帧只 drawImage —— 零 shadowBlur、零 fillText、零颜色字符串拼接。
 * 字符、颜色、透明度、字号、间距、下落速度、拖尾长度全部与原实现一致。
 */
const PAD = 8 // sprite 四周留白，容纳 6px 辉光
const FONT = `bold ${fontSize}px "JetBrains Mono", "Consolas", monospace`
const TAIL_LEN = 12 // 与原实现 for (j = 1; j < 12; ...) 一致
let cellW = 0
let cellH = 0
/** 头部字符 sprite，下标 0/1 对应字符 '0' / '1' */
let headSprites: HTMLCanvasElement[] = []
/** 拖尾字符 sprite，tailSprites[j][0|1] = 第 j 段（alpha 随 j 递减）的字符 */
let tailSprites: HTMLCanvasElement[][] = []

function makeCharSprite(ch: string, draw: (g: CanvasRenderingContext2D) => void): HTMLCanvasElement {
  // sprite 画布按 devicePixelRatio 放大后再绘制，
  // 避免「sprite 是 1×、主画布按 dpr 合成」导致高分屏上字符被拉糊。
  const c = document.createElement('canvas')
  c.width = Math.round(cellW * dpr)
  c.height = Math.round(cellH * dpr)
  const g = c.getContext('2d')!
  g.setTransform(dpr, 0, 0, dpr, 0, 0)
  g.font = FONT
  g.textBaseline = 'top'
  draw(g)
  g.fillText(ch, PAD, PAD)
  return c
}

function buildSprites() {
  cellW = fontSize + PAD * 2
  cellH = fontSize + PAD * 2
  const chars = ['0', '1']

  // 头部：青白色 + 6px 青色辉光（等价于原 shadowColor/shadowBlur 设置）
  headSprites = chars.map((ch) =>
    makeCharSprite(ch, (g) => {
      g.shadowColor = 'rgba(0, 255, 200, 0.7)'
      g.shadowBlur = 6
      g.fillStyle = 'rgba(205, 255, 248, 1)'
    }),
  )

  // 拖尾：绿色，alpha = (1 - j / 12) * 0.9，逐段递减
  tailSprites = []
  for (let j = 1; j < TAIL_LEN; j++) {
    const alpha = (1 - j / TAIL_LEN) * 0.9
    const color = `rgba(0, 200, 60, ${alpha})`
    tailSprites[j] = chars.map((ch) =>
      makeCharSprite(ch, (g) => {
        g.fillStyle = color
      }),
    )
  }
}

function pick(): number {
  return Math.random() < 0.5 ? 0 : 1
}

/** sprite 实际对应的 dpr；与当前 dpr 不一致时需要重建 */
let spriteDpr = 0

function resize() {
  if (!canvas.value) return
  // DPR 上限 2：4K + 200% 缩放时 dpr 会让画布面积翻 4 倍，
  // 而本动画是 22px 等宽字符，1~2 倍已足够清晰。
  dpr = Math.min(window.devicePixelRatio || 1, 2)
  if (spriteDpr !== dpr) {
    buildSprites() // 分辨率变化（跨屏拖动窗口 / 系统缩放改了）时重烘一遍
    spriteDpr = dpr
  }
  width = canvas.value.clientWidth
  height = canvas.value.clientHeight
  canvas.value.width = Math.round(width * dpr)
  canvas.value.height = Math.round(height * dpr)
  ctx!.setTransform(dpr, 0, 0, dpr, 0, 0) // 用 setTransform 而非 scale，避免重复调用累积

  columnCount = Math.floor(width / fontSize)
  columns = new Array(columnCount).fill(0).map(() => Math.random() * -50)
  columnSpeeds = new Array(columnCount).fill(0).map(() => 0.12 + Math.random() * 0.2)
}

function draw() {
  if (!ctx || !canvas.value) return

  // 半透明黑覆盖，制造拖尾（不透明度高 → 拖尾短、1/0 更清晰）
  ctx.fillStyle = 'rgba(0, 0, 0, 0.17)'
  ctx.fillRect(0, 0, width, height)

  for (let i = 0; i < columnCount; i++) {
    const x = Math.round(i * fontSize) - PAD
    const y = columns[i]

    // 头部字符（亮青白，辉光已烘焙在 sprite 内）
    // 显式给出目标尺寸 = CSS 像素，保证 sprite 与主画布 1:1 合成、无缩放
    ctx.drawImage(headSprites[pick()], x, Math.round(y) - PAD, cellW, cellH)

    // 尾部字符 — 绿色渐变拖尾
    for (let j = 1; j < TAIL_LEN; j++) {
      const tailY = y - j * fontSize
      if (tailY < 0) break
      ctx.drawImage(tailSprites[j][pick()], x, Math.round(tailY) - PAD, cellW, cellH)
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

/* ── 空闲时完全停帧：切到后台标签页 / 窗口最小化时不再空转 ── */
function onVisibilityChange() {
  if (document.hidden) {
    if (animationId !== null) {
      cancelAnimationFrame(animationId)
      animationId = null
    }
  } else if (animationId === null) {
    animationId = requestAnimationFrame(draw)
  }
}

/* ── resize 防抖：原实现每次都重建全部列（雨会跳变），连续拖拽窗口时每秒触发数十次 ── */
let resizeTimer: number | null = null
function onResize() {
  if (resizeTimer !== null) window.clearTimeout(resizeTimer)
  resizeTimer = window.setTimeout(() => {
    resizeTimer = null
    resize()
  }, 150)
}

onMounted(() => {
  if (!canvas.value) return
  ctx = canvas.value.getContext('2d')
  if (!ctx) return
  resize() // 首次调用内部会按当前 dpr 烘焙 sprite
  draw()
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  if (animationId !== null) cancelAnimationFrame(animationId)
  if (resizeTimer !== null) window.clearTimeout(resizeTimer)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibilityChange)
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
