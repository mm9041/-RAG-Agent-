<script setup lang="ts">
import { ref } from 'vue'

import { useAuth } from '../composables/useAuth'

const emit = defineEmits<{ success: [] }>()

const { login } = useAuth()
const password = ref('')
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!password.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    await login(password.value)
    emit('success')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <div class="login__card">
      <div class="login__brand">
        <div class="brand-dot">智</div>
        <div>
          <div class="login__title">智扫通</div>
          <div class="login__sub">扫地机器人智能客服</div>
        </div>
      </div>

      <form style="margin-top: 24px" @submit.prevent="submit">
        <label class="field__label" for="pwd">访问密码</label>
        <input
          id="pwd"
          v-model="password"
          class="input"
          type="password"
          autocomplete="current-password"
          placeholder="请输入访问密码"
          :disabled="loading"
        />
        <div v-if="error" class="notice notice--danger" style="margin: 12px 0 0">{{ error }}</div>
        <button class="btn btn--primary" style="width: 100%; margin-top: 16px" type="submit" :disabled="loading">
          <span v-if="loading" class="spin" />
          {{ loading ? '正在验证' : '登录' }}
        </button>
      </form>

      <p class="login__tip">这是演示级门禁：单一共享密码，没有账号体系。</p>
    </div>
  </div>
</template>

<style scoped>
.login {
  min-height: 100%;
  display: grid;
  place-items: center;
  background: var(--bg-side);
  padding: 24px;
}

.login__card {
  width: 100%;
  max-width: 372px;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: 30px;
}

.login__brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.login__brand .brand-dot {
  width: 38px;
  height: 38px;
  border-radius: 11px;
  font-size: 16px;
}

.login__title {
  font-size: 17px;
  font-weight: 600;
}

.login__sub {
  font-size: 12.5px;
  color: var(--text-2);
}

.login__tip {
  font-size: 11.5px;
  color: var(--text-3);
  margin: 18px 0 0;
  line-height: 1.6;
}
</style>
