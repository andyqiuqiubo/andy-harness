(() => {
  const active = document.querySelector('.session-item.active .session-title')
  const heading = document.querySelector('header h2, .banner h2, [class*="banner"] h2, h2')
  const msgs = document.querySelectorAll('.message-row')
  return {
    activeSession: active ? active.textContent.trim() : null,
    bannerText: heading ? heading.textContent.trim() : null,
    messageRows: msgs.length,
    firstMsg: msgs[0] ? msgs[0].textContent.trim().slice(0, 40) : null,
    menuOpen: !!document.querySelector('.session-menu'),
  }
})()
