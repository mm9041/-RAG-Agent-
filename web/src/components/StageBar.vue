<script setup lang="ts">
import { computed, ref } from 'vue'

import type { PhaseMark } from '../types'

const props = defineProps<{
  stage: string
  elapsed: number
  stageElapsed: number
  marks: PhaseMark[]
  stopping?: boolean
}>()

const open = ref(false)

const label = computed(() => (props.stopping ? '正在停止生成' : props.stage || '正在准备回复'))
const slow = computed(() => props.stageElapsed >= 8)
</script>

<template>
  <div class="stage">
    <span class="stage__pulse" />
    <span>{{ label }}</span>
    <button v-if="marks.length" class="stage__expand" @click="open = !open">
      {{ open ? '收起过程' : '查看过程' }}
    </button>
    <span class="stage__time">{{ elapsed.toFixed(1) }} 秒</span>
  </div>

  <div v-if="slow && !stopping" class="notice notice--info" style="margin-top: -14px">
    当前服务响应较慢，仍在处理。您可以停止生成后重试。
  </div>

  <div v-if="open && marks.length" class="stage" style="flex-direction: column; align-items: stretch">
    <div class="stage__marks" style="border-top: none; padding-top: 0; margin-top: 0">
      <div v-for="(mark, index) in marks" :key="index" class="stage__marks-row">
        <span>{{ mark.label }}</span>
        <span>{{ mark.at.toFixed(1) }}s</span>
      </div>
    </div>
  </div>
</template>
