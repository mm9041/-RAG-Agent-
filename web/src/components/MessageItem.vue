<script setup lang="ts">
import { computed } from 'vue'

import MarkdownText from './MarkdownText.vue'
import SourcesPanel from './SourcesPanel.vue'
import type { Message } from '../types'

const props = defineProps<{
  message: Message
  index: number
  threadId: string
  question: string
}>()

const emit = defineEmits<{ like: [payload: { index: number; answer: string }] }>()

const canLike = computed(
  () => props.message.role === 'assistant' && !props.message.incomplete && !!props.message.content.trim(),
)

function onLike() {
  if (!canLike.value || props.message.liked) return
  emit('like', { index: props.index, answer: props.message.content })
}
</script>

<template>
  <div v-if="message.role === 'user'" class="msg msg--user">
    <div class="bubble">{{ message.content }}</div>
  </div>

  <div v-else class="msg" :class="{ 'msg--streaming': message.pending }">
    <div class="avatar" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path d="M12 2a3 3 0 0 1 3 3v1h1a4 4 0 0 1 4 4v7a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V10a4 4 0 0 1 4-4h1V5a3 3 0 0 1 3-3Zm-3 9a1.4 1.4 0 1 0 0 2.8A1.4 1.4 0 0 0 9 11Zm6 0a1.4 1.4 0 1 0 0 2.8A1.4 1.4 0 0 0 15 11Z" />
      </svg>
    </div>

    <div class="msg__body">
      <details v-if="message.reasoning" class="reasoning">
        <summary>查看思考过程</summary>
        <div class="reasoning__body">{{ message.reasoning }}</div>
      </details>

      <div v-if="message.pending" class="msg__text msg__text--plain">{{ message.content }}</div>
      <MarkdownText v-else-if="message.content" :text="message.content" />

      <div v-if="message.failed" class="notice notice--danger" style="margin: 8px 0 0">
        本次处理未能完成，可以重试或换个问法。
      </div>

      <SourcesPanel v-if="message.sources?.length" :sources="message.sources" />

      <div v-if="!message.pending" class="msg__actions">
        <button
          v-if="canLike"
          class="icon-btn"
          :disabled="message.liked"
          :title="message.liked ? '已点赞' : '这条回复有帮助'"
          @click="onLike"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7">
            <path d="M7 22V11l4-9a2 2 0 0 1 2 2v5h5.3a2 2 0 0 1 2 2.4l-1.5 8A2 2 0 0 1 16.8 22H7Z" stroke-linejoin="round" />
            <path d="M7 11H3v11h4" stroke-linejoin="round" />
          </svg>
          {{ message.liked ? '已点赞' : '有帮助' }}
        </button>
        <span v-if="message.incomplete" class="msg__note">该轮未完成</span>
      </div>
    </div>
  </div>
</template>
