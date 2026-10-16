(() => {
  const streaming = document.querySelector('.streaming-message')
  const content = document.querySelector('.streaming-content')
  const stopBtn = document.querySelector('.btn-stop')
  const rows = [...document.querySelectorAll('.message-row.assistant')]
  const badges = [...document.querySelectorAll('.stopped-badge')]
  const lastAssistant = rows[rows.length - 1]
  return {
    isStreaming: !!streaming,
    streamingTextLen: content ? content.textContent.trim().length : 0,
    streamingTextHead: content ? content.textContent.trim().slice(0, 30) : null,
    stopBtnVisible: !!stopBtn,
    assistantRows: rows.length,
    lastAssistantText: lastAssistant ? lastAssistant.textContent.replace(/\s+/g, ' ').trim().slice(0, 60) : null,
    stoppedBadges: badges.map((b) => b.textContent.trim()),
  }
})()
