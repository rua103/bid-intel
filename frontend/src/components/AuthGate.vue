<script setup>
import { onMounted, ref } from 'vue'

const props = defineProps({ apiBase: { type: String, default: '' } })
const username = ref('reviewer')
const password = ref('')
const authEnabled = ref(false)
const authenticated = ref(false)
const checking = ref(true)
const busy = ref(false)
const error = ref('')

async function readError(response) {
  try {
    const data = await response.json()
    return typeof data.detail === 'string' ? data.detail : '登录请求失败'
  } catch { return '登录请求失败' }
}

async function refreshSession() {
  checking.value = true
  error.value = ''
  try {
    const response = await fetch(`${props.apiBase}/api/v1/auth/me`, { credentials: 'include' })
    if (!response.ok) throw new Error(await readError(response))
    const data = await response.json()
    authEnabled.value = data.auth_enabled
    authenticated.value = data.authenticated
  } catch (cause) {
    error.value = cause.message || '无法连接后端，请检查服务状态'
  } finally { checking.value = false }
}

async function login() {
  busy.value = true
  error.value = ''
  try {
    const response = await fetch(`${props.apiBase}/api/v1/auth/login`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.value, password: password.value }),
    })
    if (!response.ok) throw new Error(await readError(response))
    password.value = ''
    authenticated.value = true
    window.location.reload()
  } catch (cause) { error.value = cause.message || '登录失败，请重试' }
  finally { busy.value = false }
}

async function logout() {
  try {
    await fetch(`${props.apiBase}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' })
  } finally {
    authenticated.value = false
    window.location.reload()
  }
}

onMounted(refreshSession)
</script>

<template>
  <main v-if="checking" class="auth-loading">正在检查评审登录状态…</main>
  <main v-else-if="!authenticated" class="auth-page">
    <form class="auth-card" @submit.prevent="login">
      <p class="eyebrow">ICT 创新大赛 · 赛题五</p>
      <h1>评审账号登录</h1>
      <p class="auth-description">登录后可查看招采数据分析与人工标注工作台。</p>
      <label>用户名<input v-model="username" autocomplete="username" required /></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
      <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
      <button type="submit" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
      <button v-if="error && !authEnabled" type="button" class="secondary" @click="refreshSession">重试连接</button>
    </form>
  </main>
  <div v-else class="auth-content">
    <div v-if="authEnabled" class="auth-toolbar">
      <span>已登录：{{ username }}</span>
      <button type="button" class="secondary" @click="logout">退出登录</button>
    </div>
    <slot />
  </div>
</template>

<style scoped>
.auth-loading,.auth-page{min-height:100vh;display:grid;place-items:center;background:#f4f6f7;padding:24px}
.auth-card{width:min(100%,420px);padding:36px;border:1px solid #dce3e4;border-radius:18px;background:#fff;box-shadow:0 18px 50px #19333812}
.auth-card h1{margin:8px 0;font-size:28px}.auth-description{color:#667579;line-height:1.6;margin-bottom:24px}
.auth-card label{display:grid;gap:7px;margin:16px 0;color:#34484c;font-size:14px}
.auth-card input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #ccd8d9;border-radius:8px;font:inherit}
.auth-card button,.auth-toolbar button{padding:10px 16px;border:0;border-radius:8px;background:#176b65;color:white;font:inherit;cursor:pointer}
.auth-card button{width:100%;margin-top:10px}.auth-card button:disabled{opacity:.6;cursor:wait}
.auth-card button.secondary,.auth-toolbar button.secondary{background:#e8eeee;color:#31504e}
.auth-error{color:#aa3434;font-size:14px;line-height:1.5}
.auth-toolbar{display:flex;justify-content:flex-end;align-items:center;gap:12px;padding:8px 4vw;background:#f3f6f5;color:#526265;font-size:13px}
</style>
