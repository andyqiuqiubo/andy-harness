<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { fetchAttachmentBlobUrl } from '../api/client'

const props = defineProps<{
  sessionId: string
  attachmentId: string
  filename?: string
}>()

// P0-3：统一以 blob: 渲染附件图片，鉴权模式下也能带 Bearer 取图；
// 取图失败时回退到直链（E12 关闭时直链即生效）。
const src = ref('')
let objectUrl = ''

async function load() {
  const url = await fetchAttachmentBlobUrl(props.sessionId, props.attachmentId)
  if (objectUrl && objectUrl !== url) URL.revokeObjectURL(objectUrl)
  if (url.startsWith('blob:')) objectUrl = url
  src.value = url
}

watch(() => [props.sessionId, props.attachmentId], load, { immediate: true })

onUnmounted(() => {
  if (objectUrl) URL.revokeObjectURL(objectUrl)
})
</script>

<template>
  <img
    class="attachment-image"
    :src="src || ''"
    :alt="filename || ''"
    :title="filename || ''"
    loading="lazy"
  />
</template>
