<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import { computed } from 'vue'

const props = defineProps<{ text: string }>()

const md = new MarkdownIt({
  // 不放开原始 HTML：回答内容来自模型，等于外部输入
  html: false,
  linkify: true,
  breaks: true,
})

const html = computed(() => decorate(props.text))

/**
 * 把 [1] / [1,2] 渲染成小徽章。
 *
 * 必须跳过 <pre> / <code>：回答里偶尔出现 JSON 或下标，误替换成徽章
 * 会破坏内容。split 带捕获组时，奇数位正是被保护的分隔符。
 */
function decorate(text: string): string {
  const rendered = md.render(text)
  return rendered
    .split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g)
    .map((part, index) => {
      if (index % 2 === 1) return part
      return part.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (_match, nums: string) =>
        nums
          .split(',')
          .map((n) => `<span class="ref-chip">${n.trim()}</span>`)
          .join(''),
      )
    })
    .join('')
}
</script>

<template>
  <div class="md-body" v-html="html" />
</template>
