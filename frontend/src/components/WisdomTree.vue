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
const dpr = window.devicePixelRatio || 1

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

function resize() {
  if (!canvas.value) return
  W = canvas.value.clientWidth
  H = canvas.value.clientHeight
  canvas.value.width = W * dpr
  canvas.value.height = H * dpr
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

  // 根部地光
  const rg = ctx.createRadialGradient(rootX, rootY, 0, rootX, rootY, 120)
  rg.addColorStop(0, `rgba(0,255,180,${0.18 * Math.min(1, energy)})`)
  rg.addColorStop(1, 'rgba(0,255,180,0)')
  ctx.fillStyle = rg
  ctx.beginPath()
  ctx.arc(rootX, rootY, 120, 0, Math.PI * 2)
  ctx.fill()

  ctx.lineCap = 'round'
  for (const b of branches) {
    const tipX = b.x0 + Math.cos(b.angle) * b.len
    const tipY = b.y0 + Math.sin(b.angle) * b.len
    const a = b.split ? b.life : 1
    ctx.strokeStyle = `hsla(${b.hue}, 100%, ${58 + (b.depth % 2) * 8}%, ${a * 0.92})`
    ctx.lineWidth = b.width
    ctx.shadowColor = `hsla(${b.hue}, 100%, 65%, ${a})`
    ctx.shadowBlur = 3 + b.width
    ctx.beginPath()
    ctx.moveTo(b.x0, b.y0)
    ctx.lineTo(tipX, tipY)
    ctx.stroke()
    if (!b.split) {
      ctx.fillStyle = `hsla(${b.hue}, 100%, 80%, ${a})`
      ctx.beginPath()
      ctx.arc(tipX, tipY, b.width * 0.9, 0, Math.PI * 2)
      ctx.fill()
    }
  }
  ctx.shadowBlur = 0

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

onMounted(() => {
  if (!canvas.value) return
  ctx = canvas.value.getContext('2d')
  if (!ctx) return
  resize()
  draw()
  window.addEventListener('resize', resize)
  canvas.value.addEventListener('mousemove', onMove)
  canvas.value.addEventListener('mouseenter', onEnter)
  canvas.value.addEventListener('mouseleave', onLeave)
  canvas.value.addEventListener('mousedown', onDown)
  canvas.value.addEventListener('mouseup', onUp)
})
onUnmounted(() => {
  cancelAnimationFrame(raf)
  window.removeEventListener('resize', resize)
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
