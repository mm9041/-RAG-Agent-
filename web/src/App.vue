<script setup lang="ts">
import { onMounted } from 'vue'

import LoginGate from './components/LoginGate.vue'
import { useAuth } from './composables/useAuth'

const { me, load } = useAuth()

onMounted(() => {
  load()
})
</script>

<template>
  <div v-if="!me" class="boot-full"><span class="spin" /></div>
  <LoginGate v-else-if="me.password_required && !me.authenticated" @success="load(true)" />
  <RouterView v-else />
</template>
