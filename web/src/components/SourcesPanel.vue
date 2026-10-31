<script setup lang="ts">
import { computed, ref } from 'vue'

import type { Source } from '../types'

const props = defineProps<{ sources: Source[] }>()

const open = ref(false)
const expanded = ref<Set<number>>(new Set())

const list = computed(() => {
  const seen = new Set<string>()
  const rows: Source[] = []
  for (const source of props.sources ?? []) {
    const key = source.chunk_id ?? `${source.file}:${source.snippet}`
    if (seen.has(key)) continue
    seen.add(key)
    rows.push(source)
  }
  return rows
})

function label(source: Source): string {
  const page = source.page
  return source.file + (typeof page === 'number' ? ` · 第 ${page + 1} 页` : '')
}

function toggleSnippet(index: number) {
  const next = new Set(expanded.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  expanded.value = next
}
</script>

<template>
  <div v-if="list.length" class="sources">
    <button class="sources__head" :class="{ 'sources__head--open': open }" @click="open = !open">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6">
        <path d="M6 3l5 5-5 5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      检索参考资料 · {{ list.length }} 个片段
    </button>

    <div v-show="open" class="sources__list">
      <div v-for="(source, index) in list" :key="index" class="source">
        <div class="source__meta">
          <span class="source__file">{{ label(source) }}</span>
          <span v-if="source.distance !== undefined">距离 {{ source.distance }}</span>
          <span>引用 [{{ source.ref ?? '旧记录' }}]</span>
        </div>
        <div class="source__snippet" :class="{ 'source__snippet--open': expanded.has(index) }">
          {{ source.snippet }}
        </div>
        <button class="source__toggle" @click="toggleSnippet(index)">
          {{ expanded.has(index) ? '收起' : '展开全文' }}
        </button>
      </div>
    </div>
  </div>
</template>
