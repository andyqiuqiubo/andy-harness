<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useLanguage } from '../composables/useLanguage'
import MatrixRain from '../components/MatrixRain.vue'
import WisdomTree from '../components/WisdomTree.vue'

const { t } = useLanguage()

const titleEl = ref<HTMLElement | null>(null)
const tilt = ref({ rx: 0, ry: 0 })
const titleText = 'andy-harness'

// 文字朝鼠标方向倾斜：鼠标在哪，文字就倒向哪（绕中心做 3D 视差）
function onWindowMove(e: MouseEvent) {
  const el = titleEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const cx = rect.left + rect.width / 2
  const cy = rect.top + rect.height / 2
  const dx = (e.clientX - cx) / (window.innerWidth / 2)
  const dy = (e.clientY - cy) / (window.innerHeight / 2)
  tilt.value = { rx: -dy * 18, ry: dx * 20 }
}

onMounted(() => {
  window.addEventListener('mousemove', onWindowMove)
})
onUnmounted(() => {
  window.removeEventListener('mousemove', onWindowMove)
})
</script>

<template>
  <main class="home">
    <!-- 数字雨背景（压暗，仅作氛围） -->
    <div class="matrix-bg">
      <MatrixRain />
      <div class="matrix-vignette"></div>
    </div>

    <!-- 主角：AI智弈 -->
    <div class="hero">
      <div class="hero-badge">⚡ 智能体操控引擎</div>
      <h1
        ref="titleEl"
        class="hero-title"
        :style="{ transform: `rotateX(${tilt.rx}deg) rotateY(${tilt.ry}deg)` }"
      >
        <span v-for="(ch, i) in titleText.split('')" :key="i" class="hero-char">{{ ch }}</span>
      </h1>
      <p class="hero-subtitle">开源共筑 · 智慧自生长</p>
      <p class="hero-hint">AI智弈出品</p>
    </div>

    <!-- 交互核心：生成式思维树（纯前端，无需模型） -->
    <WisdomTree />

    <!-- 导航入口 -->
    <div class="home-nav">
      <router-link to="/chat" class="cta cta-primary">
        <span>{{ t('nav.chat') }}</span>
        <span class="cta-arrow">→</span>
      </router-link>
      <router-link to="/settings" class="cta cta-secondary">
        <span>⚙</span>
        <span>{{ t('nav.settings') }}</span>
      </router-link>
    </div>
  </main>
</template>

<style scoped>
.home {
  position: relative;
  min-height: 100vh;
  overflow: hidden;
  background: #000;
}

/* ── Matrix rain background ── */
.matrix-bg {
  position: absolute;
  inset: 0;
  z-index: 0;
  opacity: 0.45;
}
.matrix-vignette {
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse at center, transparent 0%, rgba(0, 0, 0, 0.4) 60%, rgba(0, 0, 0, 0.78) 100%);
  pointer-events: none;
}

/* ── Hero title ── */
.hero {
  position: relative;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: var(--space-lg) var(--space-lg) 0;
  perspective: 900px;
  pointer-events: none;
}
.hero > * {
  pointer-events: auto;
}

.hero-badge {
  display: inline-block;
  padding: 6px 16px;
  border-radius: var(--radius-full);
  background: rgba(0, 255, 200, 0.08);
  color: #2bffd0;
  border: 1px solid rgba(0, 255, 200, 0.25);
  font-size: 0.82rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: var(--space-md);
  backdrop-filter: blur(8px);
  text-shadow: 0 0 12px rgba(0, 255, 200, 0.5);
}

.hero-title {
  display: flex;
  gap: 0.06em;
  font-size: clamp(2.8rem, 9vw, 6.2rem);
  font-weight: 800;
  letter-spacing: 0.02em;
  line-height: 1.05;
  margin: 0;
  transform-style: preserve-3d;
  transition: transform 0.12s ease-out;
  color: #eafff8;
  text-shadow:
    0 0 24px rgba(0, 255, 200, 0.7),
    0 0 56px rgba(0, 255, 200, 0.35),
    0 0 110px rgba(0, 255, 200, 0.15);
  animation: title-glow 3.5s ease-in-out infinite;
}
.hero-char {
  display: inline-block;
  transform: translateZ(40px);
  min-width: 0.62em;
}
@keyframes title-glow {
  0%, 100% {
    text-shadow: 0 0 24px rgba(0, 255, 200, 0.6), 0 0 56px rgba(0, 255, 200, 0.3), 0 0 110px rgba(0, 255, 200, 0.12);
  }
  50% {
    text-shadow: 0 0 36px rgba(0, 255, 220, 0.85), 0 0 80px rgba(0, 255, 200, 0.45), 0 0 150px rgba(0, 255, 200, 0.22);
  }
}

.hero-subtitle {
  font-size: 1.4rem;
  color: rgba(180, 255, 235, 0.82);
  margin-top: var(--space-sm);
  font-weight: 500;
  letter-spacing: 0.2em;
  text-shadow: 0 0 12px rgba(0, 255, 200, 0.4);
}
.hero-hint {
  font-size: 1.05rem;
  font-weight: 600;
  color: #2bffd0;
  margin-top: var(--space-sm);
  letter-spacing: 0.22em;
  text-shadow: 0 0 14px rgba(0, 255, 200, 0.5);
}

/* ── 导航入口（底部） ── */
.home-nav {
  position: absolute;
  left: 50%;
  bottom: 70px;
  transform: translateX(-50%);
  z-index: 3;
  display: flex;
  gap: var(--space-md);
  flex-wrap: wrap;
  justify-content: center;
}

.cta {
  display: inline-flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-xl);
  border-radius: var(--radius-lg);
  font-size: var(--font-size-md);
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  transition: transform var(--transition-base), box-shadow var(--transition-base), background var(--transition-base), border-color var(--transition-base);
  backdrop-filter: blur(12px);
}
.cta-primary {
  background: rgba(0, 255, 200, 0.12);
  color: #2bffd0;
  border: 1px solid rgba(0, 255, 200, 0.3);
  box-shadow: 0 0 20px rgba(0, 255, 200, 0.15);
}
.cta-primary:hover {
  transform: translateY(-3px);
  background: rgba(0, 255, 200, 0.2);
  border-color: rgba(0, 255, 200, 0.55);
  box-shadow: 0 0 30px rgba(0, 255, 200, 0.35);
}
.cta-primary:active {
  transform: translateY(-1px);
}
.cta-arrow {
  transition: transform var(--transition-base);
}
.cta-primary:hover .cta-arrow {
  transform: translateX(4px);
}
.cta-secondary {
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.8);
  border: 1px solid rgba(255, 255, 255, 0.15);
}
.cta-secondary:hover {
  transform: translateY(-3px);
  background: rgba(255, 255, 255, 0.12);
  border-color: rgba(255, 255, 255, 0.3);
  color: #fff;
}
.cta-secondary:active {
  transform: translateY(-1px);
}

@media (max-width: 480px) {
  .home-nav {
    flex-direction: column;
    width: 100%;
    max-width: 280px;
  }
  .cta {
    justify-content: center;
    width: 100%;
  }
}
</style>
