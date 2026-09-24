<script setup lang="ts">
import { useLanguage } from '../composables/useLanguage'
import MatrixRain from '../components/MatrixRain.vue'

const { t } = useLanguage()
</script>

<template>
  <main class="home">
    <!-- 黑客帝国数字雨背景 -->
    <div class="matrix-bg">
      <MatrixRain />
      <div class="matrix-vignette"></div>
    </div>

    <!-- Hero content -->
    <div class="hero">
      <div class="hero-badge">⚡ AI-Powered Harness</div>
      <h1 class="hero-title">andy-harness</h1>
      <p class="hero-subtitle">{{ t('nav.chat') }}</p>
      <div class="hero-actions">
        <router-link to="/chat" class="cta cta-primary">
          <span>{{ t('nav.chat') }}</span>
          <span class="cta-arrow">→</span>
        </router-link>
        <router-link to="/settings" class="cta cta-secondary">
          <span>⚙</span>
          <span>{{ t('nav.settings') }}</span>
        </router-link>
      </div>
    </div>
  </main>
</template>

<style scoped>
.home {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  overflow: hidden;
  background: #000;
}

/* ── Matrix rain background ── */
.matrix-bg {
  position: absolute;
  inset: 0;
  z-index: 0;
}

.matrix-vignette {
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse at center, transparent 0%, rgba(0, 0, 0, 0.4) 60%, rgba(0, 0, 0, 0.8) 100%);
  pointer-events: none;
}

/* ── Hero content ── */
.hero {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: var(--space-lg);
  animation: slideUp var(--transition-slow) ease both;
}

.hero-badge {
  display: inline-block;
  padding: 6px 16px;
  border-radius: var(--radius-full);
  background: rgba(0, 255, 0, 0.08);
  color: #00ff88;
  border: 1px solid rgba(0, 255, 100, 0.2);
  font-size: var(--font-size-xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: var(--space-lg);
  backdrop-filter: blur(8px);
  text-shadow: 0 0 12px rgba(0, 255, 100, 0.5);
}

.hero-title {
  font-size: clamp(2.5rem, 6vw, 4rem);
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.1;
  margin: 0;
  color: #fff;
  text-shadow:
    0 0 20px rgba(0, 255, 100, 0.6),
    0 0 40px rgba(0, 255, 100, 0.3),
    0 0 80px rgba(0, 255, 100, 0.1);
  animation: glow-pulse 3s ease-in-out infinite;
}

@keyframes glow-pulse {
  0%, 100% {
    text-shadow:
      0 0 20px rgba(0, 255, 100, 0.6),
      0 0 40px rgba(0, 255, 100, 0.3),
      0 0 80px rgba(0, 255, 100, 0.1);
  }
  50% {
    text-shadow:
      0 0 30px rgba(0, 255, 100, 0.8),
      0 0 60px rgba(0, 255, 100, 0.4),
      0 0 100px rgba(0, 255, 100, 0.2);
  }
}

.hero-subtitle {
  font-size: var(--font-size-xl);
  color: rgba(255, 255, 255, 0.6);
  margin-top: var(--space-sm);
  font-weight: 400;
  text-shadow: 0 0 8px rgba(0, 0, 0, 0.8);
}

.hero-actions {
  display: flex;
  gap: var(--space-md);
  margin-top: var(--space-xl);
  flex-wrap: wrap;
  justify-content: center;
}

/* ── CTA buttons ── */
.cta {
  display: inline-flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-xl);
  border-radius: var(--radius-lg);
  font-size: var(--font-size-md);
  font-weight: 600;
  text-decoration: none;
  transition: transform var(--transition-base), box-shadow var(--transition-base), background var(--transition-base), border-color var(--transition-base);
  backdrop-filter: blur(12px);
}

.cta-primary {
  background: rgba(0, 255, 100, 0.12);
  color: #00ff88;
  border: 1px solid rgba(0, 255, 100, 0.3);
  box-shadow: 0 0 20px rgba(0, 255, 100, 0.15);
}

.cta-primary:hover {
  transform: translateY(-3px);
  background: rgba(0, 255, 100, 0.2);
  border-color: rgba(0, 255, 100, 0.5);
  box-shadow: 0 0 30px rgba(0, 255, 100, 0.3);
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

/* ── Responsive ── */
@media (max-width: 480px) {
  .hero-actions {
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
