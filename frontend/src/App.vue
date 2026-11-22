<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api, setUnauthorizedHandler, type AuthStatus } from '@/api/client';
import { Button } from '@/components/ui/button';
import AuthGate from './views/AuthGate.vue';
import ModulesView from './views/ModulesView.vue';
import TasksView from './views/TasksView.vue';
import AccountNetworkView from './views/AccountNetworkView.vue';
const auth = ref<AuthStatus>({ initialized: false, authenticated: false });
const loading = ref(true), error = ref('');
const tab = ref('campus');
const items = [{ id: 'campus', label: '校园事务', symbol: '◫' }, { id: 'activity', label: '执行记录', symbol: '≡' }, { id: 'settings', label: '设置', symbol: '⚙' }];
async function authenticate() { try { auth.value = await api.auth.status(); } catch (err) { error.value = err instanceof Error ? err.message : '连接失败'; } finally { loading.value = false; } }
async function logout() { try { await api.auth.logout(); auth.value.authenticated = false; } catch (err) { error.value = err instanceof Error ? err.message : '退出失败'; } }
onMounted(() => { setUnauthorizedHandler(() => { auth.value.authenticated = false; }); void authenticate(); });
</script>
<template>
  <div v-if="loading" class="auth-page">正在连接 autoBUAA…</div>
  <AuthGate v-else-if="!auth.authenticated" :initialized="auth.initialized" @authenticated="authenticate" />
  <div v-else class="app-shell">
    <aside class="sidebar"><div class="brand"><span class="brand-mark">A</span><div><p class="font-semibold tracking-tight">autoBUAA</p><p class="text-xs text-muted-foreground mt-1">校园生活助手</p></div></div><nav class="nav-list" aria-label="主导航"><Button v-for="item in items" :key="item.id" :variant="tab === item.id ? 'secondary' : 'ghost'" class="justify-start gap-3 w-full" :aria-current="tab === item.id ? 'page' : undefined" @click="tab = item.id"><span aria-hidden="true" class="text-lg w-5">{{ item.symbol }}</span>{{ item.label }}</Button></nav><div class="sidebar-footer"><span class="text-xs text-muted-foreground">autoBUAA</span><Button variant="ghost" size="sm" @click="logout">退出登录</Button></div></aside>
    <main class="main-area"><p v-if="error" role="alert" class="error-box mb-5">{{ error }}</p><ModulesView v-if="tab === 'campus'" /><TasksView v-else-if="tab === 'activity'" /><AccountNetworkView v-else /></main>
  </div>
  <p v-if="error && !auth.authenticated" role="alert" class="error-box fixed bottom-5 left-5">{{ error }}</p>
</template>
