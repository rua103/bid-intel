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
    error.value = cause instanceof TypeError
      ? '无法连接后端 API。请检查演示主机、网络、防火墙端口和 CORS 配置。'
      : cause.message || '无法连接后端，请检查服务状态'
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
  <div v-else class="auth-content annotation-app-shell">
    <div class="annotation-topbar">
      <div class="annotation-brand">
        <span class="annotation-brandmark" aria-hidden="true">标</span>
        <div>
          <strong>人工标注工作台</strong>
          <small>ICT 创新大赛 · 赛题五</small>
        </div>
      </div>
      <div class="annotation-session">
        <span class="annotation-session-status"><i aria-hidden="true"></i>{{ authEnabled ? `已登录：${username}` : '本地评测模式' }}</span>
        <button v-if="authEnabled" type="button" class="secondary" @click="logout">退出登录</button>
      </div>
    </div>
    <slot />
  </div>
</template>

<style scoped>
.auth-loading,.auth-page{min-height:100vh;display:grid;place-items:center;background:var(--annotation-workspace,#f6f7fb);padding:24px}
.auth-card{width:min(100%,420px);padding:36px;border:1px solid var(--annotation-line,#e6e2ef);border-radius:22px;background:#fff;box-shadow:0 18px 50px rgba(61,51,98,.10)}
.auth-card h1{margin:8px 0;font-size:28px;color:var(--annotation-ink,#28243a)}.auth-description{color:var(--annotation-muted,#76718a);line-height:1.6;margin-bottom:24px}
.auth-card label{display:grid;gap:7px;margin:16px 0;color:var(--annotation-ink,#28243a);font-size:14px}
.auth-card input{box-sizing:border-box;width:100%;padding:12px;border:1px solid var(--annotation-line,#e6e2ef);border-radius:11px;font:inherit;background:#fcfbff}
.auth-card input:focus{border-color:var(--annotation-violet,#7658d8);outline:3px solid rgba(118,88,216,.14);outline-offset:1px}
.auth-card button,.annotation-session button{padding:10px 16px;border:0;border-radius:999px;background:var(--annotation-violet,#7658d8);color:white;font:inherit;cursor:pointer}
.auth-card button{width:100%;margin-top:10px}.auth-card button:disabled{opacity:.6;cursor:wait}
.auth-card button.secondary,.annotation-session button.secondary{background:var(--annotation-violet-soft,#eeeaff);color:#6048b1}
.auth-error{color:#aa3434;font-size:14px;line-height:1.5}
.annotation-app-shell{min-height:100vh;background:var(--annotation-workspace,#f6f7fb);padding:14px 0 28px}
.annotation-topbar{width:min(1440px,calc(100% - 32px));min-height:58px;margin:0 auto 18px;padding:8px 12px;border:1px solid var(--annotation-line,#e6e2ef);border-radius:999px;background:rgba(255,255,255,.9);box-shadow:0 12px 35px rgba(74,55,130,.10);backdrop-filter:blur(18px);display:flex;align-items:center;justify-content:space-between;gap:16px;animation:annotation-topbar-in .34s cubic-bezier(.22,.8,.28,1) both}
.annotation-brand,.annotation-session{display:flex;align-items:center;gap:10px}.annotation-brandmark{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;background:var(--annotation-violet,#7658d8);color:#fff;font-size:14px;font-weight:800;box-shadow:0 5px 12px rgba(118,88,216,.25)}
.annotation-brand strong,.annotation-brand small{display:block}.annotation-brand strong{font-size:12px;color:#2d2940;font-weight:750}.annotation-brand small{margin-top:2px;color:#9189aa;font:9px var(--mono,'DM Mono',monospace);letter-spacing:.06em}
.annotation-session{color:#8d87a1;font-size:11px}.annotation-session-status{display:inline-flex;align-items:center;gap:8px}.annotation-session-status i{width:7px;height:7px;border-radius:999px;background:#62ba95;box-shadow:0 0 0 4px #e5f5ee}
.annotation-session button{font-size:11px;min-height:36px}
@keyframes annotation-topbar-in{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:none}}
@media(max-width:620px){.annotation-app-shell{padding-top:10px}.annotation-topbar{width:calc(100% - 20px);margin-bottom:12px;padding:8px 10px}.annotation-brand small{display:none}.annotation-session-status{font-size:10px}.annotation-session button{padding:8px 11px}}
@media(prefers-reduced-motion:reduce){.annotation-topbar{animation:none}}
</style>
