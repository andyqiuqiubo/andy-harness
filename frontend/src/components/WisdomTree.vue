<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'

interface Branch {
  x0: number
  y0: number
  angle: number
  len: number
  maxLen: number
  width: number
  depth: number
  hue: number
  life: number
  split: boolean
}

const canvas = ref<HTMLCanvasElement | null>(null)
let ctx: CanvasRenderingContext2D | null = null
let raf = 0
let W = 0
let H = 0
let dpr = Math.min(window.devicePixelRatio || 1, 2)

let branches: Branch[] = []
let rootX = 0
let rootY = 0
let rootTimer = 0

const MAX_DEPTH = 8
const SPREAD = 0.42
const GROW = 2.1
const CAP = 520

const mouse = { x: 0, y: 0, inView: false }
let pressed = false
let energy = 1

/* ── 性能：把每帧的高开销绘制换成「预计算 + 简单描边」 ──
 *
 * 旧实现的两个瓶颈（500+ 条分支时尤其明显）：
 *  1. `ctx.shadowBlur = 3 + b.width` 对**每条分支**生效（含端点光点），
 *     shadowBlur 需要独立光栅化 + 高斯模糊，是 Canvas 2D 最贵的操作之一；
 *  2. 每帧新建 RadialGradient、并为每条分支拼 alpha 颜色字符串（GC 压力）。
 *
 * 改法（颜色 / 结构 / 生长速度 / 数量上限全部不变）：
 *  - 用「一层宽而淡的外晕描边 + 一层本体描边」替代 shadowBlur，
 *    观感接近（枝条周围仍是柔和的同色辉光），成本降到零模糊；
 *  - 根部地光预渲染成一张 sprite，每帧只 drawImage，不再每帧建渐变；
 *  - hsl 颜色串按 depth 预生成（hue 只取决于 depth，共 9 种），
 *    透明度改用 globalAlpha 表达，每帧零字符串拼接。
 */
const ROOT_GLOW_R = 120
const HALO_ALPHA = 0.28 // 外晕透明度（乘在分支自身 alpha 上）
const HALO_SPREAD = 5 // 外晕比本体宽出的像素数
let rootGlow: HTMLCanvasElement | null = null

/** hue = 156 + depth * 4，depth ∈ [0, MAX_DEPTH] → 共 9 种 */
const HUES = Array.from({ length: MAX_DEPTH + 1 }, (_, d) => 156 + d * 4)
/** 本体描边色：亮度 58% / 66%（对应 depth % 2） */
const CORE_STROKE = HUES.map((h, d) => `hsl(${h}, 100%, ${58 + (d % 2) * 8}%)`)
/** 外晕描边色（原 shadowColor：hsla(hue,100%,65%,1)） */
const HALO_STROKE = HUES.map((h) => `hsl(${h}, 100%, 65%)`)
/** 端点光点填充色 */
const TIP_FILL = HUES.map((h) => `hsl(${h}, 100%, 80%)`)

function hueIndex(depth: number): number {
  return depth < 0 ? 0 : depth > MAX_DEPTH ? MAX_DEPTH : depth
}

function buildRootGlow() {
  const c = document.createElement('canvas')
  // 同样按 dpr 放大再绘制，保证与主画布 1:1 合成、不被拉伸
  c.width = Math.round(ROOT_GLOW_R * 2 * dpr)
  c.height = Math.round(ROOT_GLOW_R * 2 * dpr)
  const g = c.getContext('2d')!
  g.setTransform(dpr, 0, 0, dpr, 0, 0)
  const rg = g.createRadialGradient(ROOT_GLOW_R, ROOT_GLOW_R, 0, ROOT_GLOW_R, ROOT_GLOW_R, ROOT_GLOW_R)
  rg.addColorStop(0, 'rgba(0,255,180,0.18)')
  rg.addColorStop(1, 'rgba(0,255,180,0)')
  g.fillStyle = rg
  g.beginPath()
  g.arc(ROOT_GLOW_R, ROOT_GLOW_R, ROOT_GLOW_R, 0, Math.PI * 2)
  g.fill()
  rootGlow = c
}

/** rootGlow 实际对应的 dpr；不一致时重建 */
let rootGlowDpr = 0

function resize() {
  if (!canvas.value) return
  dpr = Math.min(window.devicePixelRatio || 1, 2)
  if (rootGlowDpr !== dpr) {
    buildRootGlow()
    rootGlowDpr = dpr
  }
  W = canvas.value.clientWidth
  H = canvas.value.clientHeight
  canvas.value.width = Math.round(W * dpr)
  canvas.value.height = Math.round(H * dpr)
  ctx!.setTransform(dpr, 0, 0, dpr, 0, 0)
  rootX = W / 2
  rootY = H * 0.9
  if (branches.length === 0) seed()
}
function seed() {
  branches = []
  for (let i = 0; i < 3; i++) spawnTrunk()
}
function angleDiff(a: number, b: number): number {
  let d = a - b
  while (d > Math.PI) d -= Math.PI * 2
  while (d < -Math.PI) d += Math.PI * 2
  return d
}
function windBias(): number {
  if (!mouse.inView) return 0
  const windA = Math.atan2(mouse.y - rootY, mouse.x - rootX)
  return windA
}
function spawnTrunk() {
  if (branches.length >= CAP) return
  const windA = windBias()
  const a = -Math.PI / 2 + (Math.random() - 0.5) * 0.35 + angleDiff(windA, -Math.PI / 2) * 0.12
  branches.push(makeBranch(rootX, rootY, a, 86, 4.6, 0))
}
function makeBranch(x0: number, y0: number, angle: number, maxLen: number, width: number, depth: number): Branch {
  return {
    x0,
    y0,
    angle,
    len: 0,
    maxLen,
    width,
    depth,
    hue: 156 + depth * 4,
    life: 1,
    split: false,
  }
}

function update() {
  const target = pressed ? 1.9 : mouse.inView ? 1.35 : 0.8
  energy += (target - energy) * 0.05
  const windA = windBias()

  rootTimer += energy
  if (rootTimer > 11) {
    rootTimer = 0
    spawnTrunk()
  }

  for (let i = branches.length - 1; i >= 0; i--) {
    const b = branches[i]
    if (!b.split) {
      b.len += GROW * energy
      if (b.len >= b.maxLen) {
        if (b.depth < MAX_DEPTH) {
          const tipX = b.x0 + Math.cos(b.angle) * b.maxLen
          const tipY = b.y0 + Math.sin(b.angle) * b.maxLen
          const childMax = b.maxLen * 0.75
          const childW = Math.max(0.6, b.width * 0.7)
          for (const da of [-SPREAD, SPREAD]) {
            if (branches.length >= CAP) break
            const ca = b.angle + da + angleDiff(windA, b.angle) * 0.2
            branches.push(makeBranch(tipX, tipY, ca, childMax, childW, b.depth + 1))
          }
        }
        b.split = true
      }
    } else {
      b.life -= 0.012 * energy
      if (b.life <= 0.02) branches.splice(i, 1)
    }
  }
}

function draw() {
  if (!ctx) return
  update()
  ctx.clearRect(0, 0, W, H)

  // 根部地光（预渲染 sprite，用 globalAlpha 表达 0.18 * min(1, energy)）
  if (rootGlow) {
    ctx.globalAlpha = Math.min(1, energy)
    ctx.drawImage(
      rootGlow,
      rootX - ROOT_GLOW_R,
      rootY - ROOT_GLOW_R,
      ROOT_GLOW_R * 2,
      ROOT_GLOW_R * 2,
    )
  }

  ctx.lineCap = 'round'
  for (const b of branches) {
    const tipX = b.x0 + Math.cos(b.angle) * b.len
    const tipY = b.y0 + Math.sin(b.angle) * b.len
    const a = b.split ? b.life : 1
    const d = hueIndex(b.depth)

    ctx.beginPath()
    ctx.moveTo(b.x0, b.y0)
    ctx.lineTo(tipX, tipY)

    // 外晕：替代原 `shadowBlur = 3 + b.width`
    ctx.globalAlpha = a * HALO_ALPHA
    ctx.strokeStyle = HALO_STROKE[d]
    ctx.lineWidth = b.width + HALO_SPREAD
    ctx.stroke()

    // 本体（原 strokeStyle 的 alpha = a * 0.92）
    ctx.globalAlpha = a * 0.92
    ctx.strokeStyle = CORE_STROKE[d]
    ctx.lineWidth = b.width
    ctx.stroke()

    if (!b.split) {
      // 端点光点（原来同样带 shadow 辉光，这里补齐外晕 + 实心两层）
      ctx.globalAlpha = a * HALO_ALPHA
      ctx.fillStyle = HALO_STROKE[d]
      ctx.beginPath()
      ctx.arc(tipX, tipY, b.width * 0.9 + HALO_SPREAD * 0.5, 0, Math.PI * 2)
      ctx.fill()

      ctx.globalAlpha = a
      ctx.fillStyle = TIP_FILL[d]
      ctx.beginPath()
      ctx.arc(tipX, tipY, b.width * 0.9, 0, Math.PI * 2)
      ctx.fill()
    }
  }
  ctx.globalAlpha = 1

  raf = requestAnimationFrame(draw)
}

function onMove(e: MouseEvent) {
  if (!canvas.value) return
  const rect = canvas.value.getBoundingClientRect()
  mouse.x = e.clientX - rect.left
  mouse.y = e.clientY - rect.top
  mouse.inView = true
}
function onEnter() {
  mouse.inView = true
}
function onLeave() {
  mouse.inView = false
}
function onDown() {
  pressed = true
}
function onUp() {
  pressed = false
}

/* ── 空闲时完全停帧：切到后台标签页 / 窗口最小化时不再空转 ── */
function onVisibilityChange() {
  if (document.hidden) {
    if (raf) {
      cancelAnimationFrame(raf)
      raf = 0
    }
  } else if (!raf) {
    raf = requestAnimationFrame(draw)
  }
}

/* ── resize 防抖：连续拖拽窗口时别每像素重建画布 ── */
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
  resize() // 首次调用内部会按当前 dpr 烘焙地光 sprite
  draw()
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibilityChange)
  canvas.value.addEventListener('mousemove', onMove)
  canvas.value.addEventListener('mouseenter', onEnter)
  canvas.value.addEventListener('mouseleave', onLeave)
  canvas.value.addEventListener('mousedown', onDown)
  canvas.value.addEventListener('mouseup', onUp)
})
onUnmounted(() => {
  if (raf) cancelAnimationFrame(raf)
  if (resizeTimer !== null) window.clearTimeout(resizeTimer)
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  if (canvas.value) {
    canvas.value.removeEventListener('mousemove', onMove)
    canvas.value.removeEventListener('mouseenter', onEnter)
    canvas.value.removeEventListener('mouseleave', onLeave)
    canvas.value.removeEventListener('mousedown', onDown)
    canvas.value.removeEventListener('mouseup', onUp)
  }
})
</script>

<template>
  <canvas ref="canvas" class="wisdom-tree"></canvas>
</template>

<style scoped>
.wisdom-tree {
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  z-index: 1;
  cursor: crosshair;
}
</style>
