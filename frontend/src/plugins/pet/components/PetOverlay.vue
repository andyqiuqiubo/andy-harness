<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'

// ─────────────────────────────────────────────
// 元气宠物（全局悬浮层版）：常驻在会话页/所有页面之上，
// 会在页面里自由走动，悬停会卖萌，不拦截下方页面操作。
// 状态保存在 localStorage，离线也会随时间衰减。
// ─────────────────────────────────────────────

const STORAGE_KEY = 'harness.pet.state'
const STAGE_NAMES = ['幼崽期', '成长期', '成熟期']
const STAGE_COLORS = ['#ffd166', '#74c0fc', '#b197fc']
const POKE_LINES = ['喵~?', '嘿嘿，怎么啦？', '摸摸我嘛～', '今天也要元气满满！', '咕噜咕噜……', '陪我玩陪我玩！']
const HOVER_REACTIONS = ['re-excited', 're-wink', 're-jump', 're-spin', 're-blush']
const HOVER_EMOJIS = ['💕', '⭐', '✨', '😍', '💫']

// 走动边界：四周留白 + 底部预留（聊天输入框/控制面板）
const EDGE_MARGIN = 50
const BOTTOM_RESERVE = 150

interface PetState {
  hunger: number
  happiness: number
  energy: number
  care: number
  lastUpdated: number
}

function defaultState(): PetState {
  return { hunger: 80, happiness: 80, energy: 80, care: 0, lastUpdated: Date.now() }
}

function clamp(v: number): number {
  return Math.max(0, Math.min(100, Math.round(v)))
}

function loadState(): PetState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaultState()
    const s = JSON.parse(raw) as PetState
    const elapsedMin = Math.min((Date.now() - (s.lastUpdated || Date.now())) / 60000, 180)
    s.hunger = clamp(s.hunger - elapsedMin * 0.9)
    s.happiness = clamp(s.happiness - elapsedMin * 0.5)
    s.energy = clamp(s.energy + elapsedMin * 0.3)
    return s
  } catch {
    return defaultState()
  }
}

const state = reactive<PetState>(loadState())
const sleeping = ref(false)
const bubble = ref('')
const particles = ref<{ id: number; emoji: string; x: number }[]>([])
const soundOn = ref(true)
const panelOpen = ref(true)

const petPos = reactive({ x: 0, y: 0 })
const moveDur = ref(0)
const facing = ref(1)
const walking = ref(false)
const hoverReaction = ref('')

let bubbleTimer: number | undefined
let saveTimer: number | undefined
let roamTimer: number | undefined
let pid = 0

const mood = computed<'happy' | 'hungry' | 'sad' | 'sleep'>(() => {
  if (sleeping.value) return 'sleep'
  if (state.hunger < 25) return 'hungry'
  if (state.happiness < 25) return 'sad'
  return 'happy'
})

const stage = computed(() => (state.care >= 60 ? 2 : state.care >= 20 ? 1 : 0))
const stageName = computed(() => STAGE_NAMES[stage.value])
const stageColor = computed(() => STAGE_COLORS[stage.value])
const petSizeNum = computed(() => 62 + stage.value * 14)
const petSize = computed(() => petSizeNum.value + 'px')
const petStyle = computed(() => ({ '--pet-size': petSize.value, '--pet-color': stageColor.value }))
const bubbleStyle = computed(() => ({
  left: petPos.x + petSizeNum.value / 2 + 'px',
  top: petPos.y - 30 + 'px',
}))

function say(text: string, ms = 2200) {
  bubble.value = text
  if (bubbleTimer) window.clearTimeout(bubbleTimer)
  bubbleTimer = window.setTimeout(() => (bubble.value = ''), ms)
}

function burst(emoji: string, count = 6) {
  for (let i = 0; i < count; i++) {
    const id = ++pid
    particles.value.push({ id, emoji, x: 30 + Math.random() * 40 })
    window.setTimeout(() => {
      particles.value = particles.value.filter((p) => p.id !== id)
    }, 1100)
  }
}

let actx: AudioContext | null = null
function blip(freq = 440, dur = 0.09, type: OscillatorType = 'sine') {
  if (!soundOn.value) return
  try {
    actx ??= new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)()
    if (actx.state === 'suspended') void actx.resume()
    const osc = actx.createOscillator()
    const gain = actx.createGain()
    osc.type = type
    osc.frequency.value = freq
    gain.gain.setValueAtTime(0.06, actx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.0001, actx.currentTime + dur)
    osc.connect(gain)
    gain.connect(actx.destination)
    osc.start()
    osc.stop(actx.currentTime + dur)
  } catch {
    /* 忽略音频错误 */
  }
}

function cuteSound() {
  const f = 500 + Math.random() * 450
  blip(f, 0.07, 'triangle')
  window.setTimeout(() => blip(f * 1.5, 0.06, 'sine'), 90)
}

// ── 自由走动 ──
function clampToBounds() {
  const size = petSizeNum.value
  const vw = window.innerWidth
  const vh = window.innerHeight
  const maxX = Math.max(vw - size - EDGE_MARGIN, EDGE_MARGIN)
  const maxY = Math.max(vh - size - BOTTOM_RESERVE, EDGE_MARGIN)
  petPos.x = Math.min(Math.max(petPos.x, EDGE_MARGIN), maxX)
  petPos.y = Math.min(Math.max(petPos.y, EDGE_MARGIN), maxY)
}

function moveToRandom() {
  if (sleeping.value) return
  const size = petSizeNum.value
  const vw = window.innerWidth
  const vh = window.innerHeight
  const maxX = vw - size - EDGE_MARGIN * 2
  const maxY = vh - size - BOTTOM_RESERVE - EDGE_MARGIN
  if (maxX <= 0 || maxY <= 0) return
  const tx = EDGE_MARGIN + Math.random() * maxX
  const ty = EDGE_MARGIN + Math.random() * maxY
  const dist = Math.hypot(tx - petPos.x, ty - petPos.y)
  const dur = Math.min(Math.max(dist / 120, 1.2), 4.5)
  facing.value = tx >= petPos.x ? 1 : -1
  walking.value = true
  moveDur.value = dur
  petPos.x = tx
  petPos.y = ty
  if (roamTimer) window.clearTimeout(roamTimer)
  roamTimer = window.setTimeout(() => {
    walking.value = false
    if (Math.random() < 0.5) triggerReaction(true)
    roamTimer = window.setTimeout(moveToRandom, 1300 + Math.random() * 2600)
  }, dur * 1000)
}

function startRoaming() {
  if (roamTimer) window.clearTimeout(roamTimer)
  clampToBounds()
  roamTimer = window.setTimeout(moveToRandom, 900 + Math.random() * 1500)
}

function stopRoaming() {
  if (roamTimer) window.clearTimeout(roamTimer)
  walking.value = false
}

// ── 悬停卖萌 / 随机动作 ──
function triggerReaction(quiet = false) {
  if (hoverReaction.value) return
  hoverReaction.value = HOVER_REACTIONS[Math.floor(Math.random() * HOVER_REACTIONS.length)]
  burst(HOVER_EMOJIS[Math.floor(Math.random() * HOVER_EMOJIS.length)], 3)
  if (!quiet) cuteSound()
  window.setTimeout(() => (hoverReaction.value = ''), 950)
}

function onHover() {
  if (sleeping.value) {
    burst('💤', 2)
    blip(220, 0.12, 'sine')
    say('zzz……别吵我嘛', 1600)
    return
  }
  triggerReaction(false)
}

// ── 互动操作 ──
function feed() {
  if (sleeping.value) wake()
  if (state.hunger >= 95) {
    say('我吃饱啦，肚子圆滚滚的！')
    blip(300, 0.08, 'triangle')
    return
  }
  state.hunger = clamp(state.hunger + 22)
  state.happiness = clamp(state.happiness + 6)
  state.energy = clamp(state.energy + 4)
  state.care += 3
  burst('🍎', 4)
  burst('😋', 3)
  say('啊呜啊呜……真香！')
  blip(520, 0.1, 'triangle')
  save()
}

function play() {
  if (sleeping.value) wake()
  if (state.energy < 15) {
    say('我没力气了……让我睡一会儿吧')
    blip(200, 0.1, 'sine')
    return
  }
  state.happiness = clamp(state.happiness + 20)
  state.energy = clamp(state.energy - 15)
  state.hunger = clamp(state.hunger - 5)
  state.care += 4
  burst('🎉', 5)
  burst('✨', 4)
  say('耶！再来一次！')
  blip(660, 0.12, 'square')
  window.setTimeout(() => blip(880, 0.1, 'square'), 120)
  save()
}

function pet() {
  if (sleeping.value) {
    say('zzz……（睡着啦）')
    return
  }
  state.happiness = clamp(state.happiness + 12)
  state.care += 2
  burst('💕', 6)
  say('呼噜呼噜～好舒服！')
  blip(440, 0.08, 'sine')
  save()
}

function wake() {
  sleeping.value = false
  say('……唔？早上好呀！')
  startRoaming()
}

function toggleSleep() {
  if (sleeping.value) {
    wake()
    blip(500, 0.08, 'triangle')
  } else {
    sleeping.value = true
    stopRoaming()
    state.energy = clamp(state.energy + 5)
    burst('💤', 3)
    say('晚安啦～')
    blip(330, 0.12, 'sine')
    save()
  }
}

function poke() {
  if (sleeping.value) {
    say('zzz……别吵我嘛')
    burst('💤', 2)
    return
  }
  say(POKE_LINES[Math.floor(Math.random() * POKE_LINES.length)], 1600)
  blip(700 + Math.random() * 200, 0.06, 'sine')
  burst('💫', 2)
}

// ── 存档与衰减 ──
function save() {
  state.lastUpdated = Date.now()
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
}

function tick() {
  const rate = sleeping.value ? 0.35 : 1
  state.hunger = clamp(state.hunger - 2.2 * rate)
  state.happiness = clamp(state.happiness - 1.1 * rate)
  if (sleeping.value) state.energy = clamp(state.energy + 4)
  if (!sleeping.value && Math.random() < 0.18) {
    if (state.hunger < 25) say('我好饿呀……给我点吃的嘛')
    else if (state.happiness < 25) say('好无聊……陪我玩玩吧')
  }
  save()
}

function onResize() {
  clampToBounds()
}

onMounted(() => {
  petPos.x = window.innerWidth / 2 - petSizeNum.value / 2
  petPos.y = Math.max(window.innerHeight - petSizeNum.value - BOTTOM_RESERVE - 30, EDGE_MARGIN)
  saveTimer = window.setInterval(tick, 10000)
  window.addEventListener('resize', onResize)
  startRoaming()
  if (state.hunger < 30) say('我饿了……', 3000)
})

onUnmounted(() => {
  if (saveTimer) window.clearInterval(saveTimer)
  if (bubbleTimer) window.clearTimeout(bubbleTimer)
  if (roamTimer) window.clearTimeout(roamTimer)
  window.removeEventListener('resize', onResize)
  save()
})
</script>

<template>
  <div class="pet-overlay">
    <!-- 宠物本体（可走动，可交互） -->
    <div
      class="pet-wrap"
      :style="{
        left: petPos.x + 'px',
        top: petPos.y + 'px',
        transform: `scaleX(${facing})`,
        transition: `left ${moveDur}s ease-in-out, top ${moveDur}s ease-in-out`,
      }"
      @mouseenter="onHover"
      @click="poke"
    >
      <div class="pet" :class="[mood, hoverReaction, { walking, sleeping }]" :style="petStyle">
        <div class="body"></div>
        <div class="eye left"></div>
        <div class="eye right"></div>
        <div class="blush left"></div>
        <div class="blush right"></div>
        <div class="mouth"></div>
        <div class="paw left"></div>
        <div class="paw right"></div>
        <div v-if="sleeping" class="zzz">Z</div>
        <div v-if="sleeping" class="zzz z2">Z</div>
        <div v-if="sleeping" class="zzz z3">Z</div>
      </div>
      <div class="pet-shadow"></div>
      <div v-for="p in particles" :key="p.id" class="particle" :style="{ left: p.x + '%' }">{{ p.emoji }}</div>
    </div>

    <!-- 跟随宠物的气泡 -->
    <transition name="pop">
      <div v-if="bubble" class="bubble" :style="bubbleStyle">{{ bubble }}</div>
    </transition>

    <!-- 紧凑控制面板（右下角） -->
    <div class="pet-panel">
      <div class="panel-head">
        <span class="panel-name">元气伙伴</span>
        <span class="stage-badge" :style="{ background: stageColor }">{{ stageName }}</span>
        <button class="panel-btn" :title="soundOn ? '关闭音效' : '开启音效'" @click="soundOn = !soundOn">
          {{ soundOn ? '🔊' : '🔇' }}
        </button>
        <button class="panel-btn" :title="panelOpen ? '收起面板' : '展开面板'" @click="panelOpen = !panelOpen">
          {{ panelOpen ? '🔼' : '🔽' }}
        </button>
      </div>
      <div v-if="panelOpen" class="panel-body">
        <div class="stats">
          <div class="stat">
            <span class="stat-icon">🍎</span>
            <div class="stat-bar"><div class="stat-fill hunger" :style="{ width: state.hunger + '%' }"></div></div>
            <span class="stat-val">{{ state.hunger }}</span>
          </div>
          <div class="stat">
            <span class="stat-icon">💖</span>
            <div class="stat-bar"><div class="stat-fill happiness" :style="{ width: state.happiness + '%' }"></div></div>
            <span class="stat-val">{{ state.happiness }}</span>
          </div>
          <div class="stat">
            <span class="stat-icon">⚡</span>
            <div class="stat-bar"><div class="stat-fill energy" :style="{ width: state.energy + '%' }"></div></div>
            <span class="stat-val">{{ state.energy }}</span>
          </div>
        </div>
        <div class="actions">
          <button class="action-btn" title="喂食" @click="feed"><span class="act-icon">🍎</span>喂食</button>
          <button class="action-btn" title="玩耍" @click="play"><span class="act-icon">🎾</span>玩耍</button>
          <button class="action-btn" title="抚摸" @click="pet"><span class="act-icon">💕</span>抚摸</button>
          <button class="action-btn" title="睡觉/叫醒" @click="toggleSleep"><span class="act-icon">{{ sleeping ? '☀️' : '😴' }}</span>{{ sleeping ? '叫醒' : '睡觉' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 悬浮层整体不拦截页面事件，只有宠物与面板可交互 */
.pet-overlay {
  position: fixed;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
  z-index: 9001;
}

.pet-wrap {
  position: absolute;
  cursor: pointer;
  user-select: none;
  will-change: left, top;
  z-index: 1;
  pointer-events: auto;
}

/* ── 宠物本体 ── */
.pet {
  position: relative;
  width: var(--pet-size, 76px);
  height: calc(var(--pet-size, 76px) * 0.9);
  animation: idle 3.2s ease-in-out infinite;
}
@keyframes idle {
  0%, 100% { transform: translateY(0) rotate(0deg); }
  30% { transform: translateY(-5px) rotate(-2deg); }
  60% { transform: translateY(0) rotate(2deg); }
}
.pet.sleeping { animation: sleepBreath 3.5s ease-in-out infinite; }
@keyframes sleepBreath {
  0%, 100% { transform: scale(1, 1); }
  50% { transform: scale(1.05, 0.96); }
}

.pet.walking { animation: walkBob 0.45s ease-in-out infinite; }
@keyframes walkBob {
  0%, 100% { transform: translateY(0) rotate(-3deg); }
  50% { transform: translateY(-5px) rotate(3deg); }
}
.pet.walking .body { animation: bodySquash 0.45s ease-in-out infinite; }
@keyframes bodySquash {
  0%, 100% { transform: scale(1, 1); }
  50% { transform: scale(1.04, 0.94); }
}
.pet.walking .paw { animation: pawStep 0.45s ease-in-out infinite; }
.pet.walking .paw.right { animation-delay: 0.22s; }
@keyframes pawStep {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-3px); }
}

.body {
  position: absolute;
  inset: 0;
  border-radius: 50% 50% 46% 46% / 54% 54% 46% 46%;
  background: radial-gradient(circle at 35% 28%, color-mix(in srgb, var(--pet-color) 80%, #fff), var(--pet-color) 55%, color-mix(in srgb, var(--pet-color) 70%, #222));
  box-shadow: inset -8px -10px 18px rgba(0, 0, 0, 0.12), inset 6px 6px 12px rgba(255, 255, 255, 0.35);
}

.eye {
  position: absolute;
  top: 34%;
  width: 15%;
  height: 22%;
  background: #fff;
  border-radius: 50%;
  box-shadow: inset 0 -1px 2px rgba(0, 0, 0, 0.1);
  transition: transform 0.2s;
}
.eye::after {
  content: '';
  position: absolute;
  inset: 22% 24%;
  background: #2b2b2b;
  border-radius: 50%;
  transition: transform 0.15s;
}
.eye.left { left: 24%; }
.eye.right { right: 24%; }
.pet.happy .eye { transform: scaleY(0.45); }
.pet.hungry .eye { top: 38%; transform: scaleY(1.05); }
.pet.sad .eye::after { top: 50%; }
.pet.sad .eye { transform: translateY(6%); }
.pet.sleeping .eye::after { display: none; }
.pet.sleeping .eye {
  top: 38%;
  height: 6%;
  background: #2b2b2b;
  border-radius: 999px;
}

.blush {
  position: absolute;
  top: 48%;
  width: 13%;
  height: 8%;
  background: rgba(255, 120, 140, 0.4);
  border-radius: 50%;
  opacity: 0;
  transition: opacity 0.3s;
}
.blush.left { left: 15%; }
.blush.right { right: 15%; }
.pet.happy .blush { opacity: 1; }

.mouth {
  position: absolute;
  bottom: 24%;
  left: 50%;
  width: 20%;
  height: 10%;
  transform: translateX(-50%);
}
.pet.happy .mouth {
  border-bottom: 3px solid #2b2b2b;
  border-radius: 0 0 20px 20px;
}
.pet.sad .mouth {
  border-top: 3px solid #2b2b2b;
  border-radius: 20px 20px 0 0;
}
.pet.hungry .mouth {
  width: 10%;
  height: 10%;
  border: 2.5px solid #2b2b2b;
  border-radius: 50%;
  background: transparent;
}
.pet.sleeping .mouth { border-bottom: 2px solid #2b2b2b; border-radius: 0 0 10px 10px; }

.paw {
  position: absolute;
  bottom: 6%;
  width: 16%;
  height: 12%;
  background: color-mix(in srgb, var(--pet-color) 75%, #fff);
  border-radius: 45% 45% 50% 50%;
}
.paw.left { left: 8%; transform: rotate(8deg); }
.paw.right { right: 8%; transform: rotate(-8deg); }
.pet.happy .paw { animation: pawWag 0.8s ease-in-out infinite; }
.pet.happy .paw.right { animation: pawWag 0.8s ease-in-out infinite reverse; }
@keyframes pawWag {
  0%, 100% { transform: rotate(8deg); }
  50% { transform: rotate(-6deg); }
}

/* 悬停随机反应 */
.pet.re-jump { animation: reactJump 0.95s ease; }
@keyframes reactJump {
  0%, 100% { transform: translateY(0); }
  35% { transform: translateY(-32px); }
  70% { transform: translateY(4px); }
}
.pet.re-spin { animation: reactSpin 0.7s ease; }
@keyframes reactSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.pet.re-excited .body { animation: excitedBounce 0.4s ease-in-out infinite; }
@keyframes excitedBounce {
  0%, 100% { transform: scale(1, 1); }
  50% { transform: scale(1.14, 0.9); }
}
.pet.re-wink .eye.left::after { transform: scaleY(0.3); }
.pet.re-wink .blush.left, .pet.re-wink .blush.right { opacity: 1; }
.pet.re-blush .blush { opacity: 1; background: rgba(255, 90, 120, 0.55); }

.zzz {
  position: absolute;
  right: -14%;
  font-size: 1rem;
  font-weight: 700;
  color: #8899cc;
  opacity: 0;
  animation: zzzFloat 2.2s ease-in-out infinite;
}
.zzz.z2 { right: -22%; top: 6%; animation-delay: 0.7s; font-size: 0.85rem; }
.zzz.z3 { right: -6%; top: -16%; animation-delay: 1.4s; font-size: 0.7rem; }
@keyframes zzzFloat {
  0% { opacity: 0; transform: translateY(8px); }
  30% { opacity: 1; }
  100% { opacity: 0; transform: translateY(-14px); }
}

.pet-shadow {
  position: absolute;
  left: 15%;
  right: 15%;
  bottom: -7px;
  height: 12px;
  background: radial-gradient(closest-side, rgba(0, 0, 0, 0.2), transparent);
  animation: shadowPulse 3.2s ease-in-out infinite;
}
@keyframes shadowPulse {
  0%, 100% { transform: scaleX(1); opacity: 1; }
  30% { transform: scaleX(0.86); opacity: 0.7; }
}

.particle {
  position: absolute;
  bottom: 40%;
  font-size: 1.1rem;
  pointer-events: none;
  animation: floatUp 1.05s ease-out forwards;
}
@keyframes floatUp {
  0% { opacity: 0; transform: translateY(0) scale(0.6); }
  20% { opacity: 1; }
  100% { opacity: 0; transform: translateY(-80px) scale(1.25); }
}

.bubble {
  position: absolute;
  transform: translateX(-50%);
  background: var(--bg-surface, #fff);
  border: 1px solid var(--border-color, #ddd);
  border-radius: 14px;
  padding: 4px 12px;
  font-size: 0.8rem;
  color: var(--color-text, #333);
  white-space: nowrap;
  box-shadow: var(--shadow-md, 0 4px 12px rgba(0, 0, 0, 0.1));
  z-index: 3;
  pointer-events: none;
}
.bubble::after {
  content: '';
  position: absolute;
  left: 50%;
  bottom: -6px;
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: var(--bg-surface, #fff);
  border-bottom: 0;
}
.pop-enter-active { transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1); }
.pop-leave-active { transition: all 0.15s ease; }
.pop-enter-from, .pop-leave-to { opacity: 0; transform: translateX(-50%) translateY(6px) scale(0.8); }

/* ── 紧凑控制面板 ── */
.pet-panel {
  position: absolute;
  right: 14px;
  bottom: 14px;
  z-index: 2;
  pointer-events: auto;
  min-width: 168px;
  padding: 8px 10px;
  background: color-mix(in srgb, var(--bg-surface, #fff) 88%, transparent);
  backdrop-filter: blur(8px);
  border: 1px solid var(--border-color, #ddd);
  border-radius: 14px;
  box-shadow: var(--shadow-lg, 0 8px 24px rgba(0, 0, 0, 0.14));
}
.panel-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.panel-name { font-size: 0.85rem; font-weight: 700; color: var(--color-text, #333); flex: 1; }
.stage-badge {
  padding: 1px 8px;
  border-radius: 999px;
  color: #fff;
  font-size: 0.68rem;
  font-weight: 600;
}
.panel-btn {
  border: none;
  background: var(--bg-hover, #eef);
  border-radius: 50%;
  width: 24px;
  height: 24px;
  font-size: 0.8rem;
  cursor: pointer;
  opacity: 0.8;
  padding: 0;
  line-height: 1;
}
.panel-btn.muted { opacity: 0.35; }

.panel-body { display: flex; flex-direction: column; gap: 7px; margin-top: 8px; }

.stats { display: flex; flex-direction: column; gap: 4px; }
.stat { display: flex; align-items: center; gap: 6px; }
.stat-icon { font-size: 0.85rem; width: 1.2rem; text-align: center; }
.stat-bar { flex: 1; height: 7px; background: var(--bg-hover, #eceff5); border-radius: 999px; overflow: hidden; }
.stat-fill { height: 100%; border-radius: 999px; transition: width 0.5s ease; }
.stat-fill.hunger { background: linear-gradient(90deg, #ffa94d, #ff6b6b); }
.stat-fill.happiness { background: linear-gradient(90deg, #ff9ed2, #ff6b9d); }
.stat-fill.energy { background: linear-gradient(90deg, #ffd43b, #ff9f1a); }
.stat-val { font-size: 0.72rem; color: var(--color-text-secondary, #888); width: 1.7rem; text-align: right; }

.actions { display: flex; gap: 5px; flex-wrap: wrap; justify-content: center; }
.action-btn {
  display: flex;
  align-items: center;
  gap: 3px;
  padding: 4px 10px;
  border: 1px solid var(--border-color, #ddd);
  border-radius: 999px;
  background: var(--bg-surface, #fff);
  color: var(--color-text, #333);
  font-size: 0.78rem;
  cursor: pointer;
  transition: transform 0.15s, box-shadow 0.15s, background 0.15s;
}
.action-btn:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-md, 0 4px 12px rgba(0, 0, 0, 0.12));
  background: var(--bg-hover, #f0f2f8);
}
.action-btn:active { transform: translateY(0) scale(0.96); }
.act-icon { font-size: 0.9rem; }
</style>
