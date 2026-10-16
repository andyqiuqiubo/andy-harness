(() => {
  const m = document.querySelector('.session-menu')
  if (!m) return { ok: false, reason: 'menu not found' }
  const r = m.getBoundingClientRect()
  const inView = (b) => b.bottom <= innerHeight && b.top >= 0 && b.right <= innerWidth && b.left >= 0
  const d = m.querySelector('.menu-item.danger')
  const dr = d ? d.getBoundingClientRect() : null
  return {
    ok: true,
    viewport: { w: innerWidth, h: innerHeight },
    menu: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, w: r.width, h: r.height },
    menuFullyInViewport: inView(r),
    dangerVisible: !!dr && inView(dr),
    dangerText: d ? (d.querySelector('.menu-label') || {}).textContent : null,
    items: [...m.querySelectorAll('.menu-item')].map((x) => ({
      label: (x.querySelector('.menu-label') || {}).textContent,
      visible: inView(x.getBoundingClientRect()),
    })),
  }
})()
