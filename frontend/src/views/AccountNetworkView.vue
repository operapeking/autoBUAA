<script setup lang="ts">
import { reactive, ref } from 'vue';
import { api, networkNames, type AccountInfo, type CampusOption, type NetworkMode } from '@/api/client';
import { useLive } from '@/composables/useLive';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import NotificationsView from './NotificationsView.vue';
const account = ref<AccountInfo | null>(null);
const form = reactive({ student_id: '', password: '', campus: '北京' as CampusOption, network_mode: 'webvpn' as NetworkMode });
const captcha = ref(''), error = ref(''), message = ref(''), busy = ref(false);
const passwords = reactive({ old: '', next: '' });
let initialized = false;
const { error: loadError, refresh } = useLive(async () => {
  const [acc, net] = await Promise.all([api.account.get(), api.network.get()]); account.value = acc;
  if (!initialized) { form.student_id = acc.student_id; form.campus = acc.campus; form.network_mode = net.network_mode; initialized = true; }
});
async function act(fn: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true; error.value = ''; message.value = '';
  try { await fn(); } catch (err) { error.value = err instanceof Error ? err.message : '操作失败'; }
  finally { busy.value = false; await refresh(); }
}
async function saveAndLogin() {
  await api.account.update({ student_id: form.student_id, campus: form.campus, enabled: true, password: form.password || undefined });
  await api.network.setMode(form.network_mode); form.password = '';
  const result = await api.account.login();
  message.value = result.ok ? '校园账号已连接' : result.message || '请输入验证码';
}
const authNames: Record<string, string> = { authenticated: '已连接', configured: '已配置', unconfigured: '等待配置', captcha_required: '需要验证码', failed: '登录失败', expired: '登录已过期' };
</script>
<template>
  <div class="space-y-7"><header><h1>设置</h1></header>
    <p v-if="error || loadError" role="alert" class="error-box">{{ error || loadError }}</p><p v-if="message" role="status" class="success-box">{{ message }}</p>
    <Card><CardHeader><CardTitle>校园账号 <span class="ml-3 text-sm font-normal text-muted-foreground">{{ authNames[account?.auth_status || ''] || account?.auth_status }}</span></CardTitle></CardHeader><CardContent>
      <form class="space-y-5" @submit.prevent="act(saveAndLogin)"><div class="grid gap-5 sm:grid-cols-2">
        <div class="field"><label for="student">学号</label><Input id="student" v-model="form.student_id" required autocomplete="username" /></div>
        <div class="field"><label for="campus-password">统一认证密码</label><Input id="campus-password" v-model="form.password" type="password" autocomplete="current-password" :required="!account?.has_password" :placeholder="account?.has_password ? '留空保留已保存密码' : ''" /></div>
        <div class="field"><label for="campus">校区</label><select id="campus" v-model="form.campus"><option value="北京">北京（学院路与沙河）</option><option value="学院路">学院路</option><option value="沙河">沙河</option><option value="杭州">杭州</option></select></div>
        <div class="field"><label for="network">网络连接</label><select id="network" v-model="form.network_mode"><option v-for="(name, mode) in networkNames" :key="mode" :value="mode">{{ name }}</option></select></div>
      </div><p v-if="account?.auth_error" class="text-sm text-destructive">{{ account.auth_error }}</p><Button :disabled="busy || !initialized" type="submit">{{ busy ? '正在连接…' : '保存并连接' }}</Button></form>
      <form v-if="account?.auth_status === 'captcha_required'" class="mt-6 flex gap-3 items-center flex-wrap" @submit.prevent="act(async () => { const result = await api.account.submitCaptcha(captcha); if (!result.ok) throw new Error(result.message || '验证码认证失败'); captcha = ''; message = '校园账号已连接'; })"><img v-if="account.has_captcha_image" :src="`/api/v1/account/captcha/image?v=${account.updated_at}`" alt="统一认证验证码" class="h-12 rounded border" /><Input v-model="captcha" aria-label="验证码" class="w-36" required placeholder="验证码" /><Button type="submit" :disabled="busy">提交验证码</Button></form>
    </CardContent></Card>
    <NotificationsView />
    <Card><CardHeader><CardTitle>管理员密码</CardTitle></CardHeader><CardContent><form class="flex flex-wrap gap-3" @submit.prevent="act(async () => { await api.auth.changePassword(passwords.old, passwords.next); passwords.old = ''; passwords.next = ''; message = '管理员密码已更新'; })"><Input v-model="passwords.old" aria-label="当前管理员密码" type="password" autocomplete="current-password" required placeholder="当前密码" class="sm:w-52" /><Input v-model="passwords.next" aria-label="新管理员密码" type="password" autocomplete="new-password" minlength="6" required placeholder="新密码（至少 6 位）" class="sm:w-52" /><Button variant="outline" type="submit" :disabled="busy">修改密码</Button></form></CardContent></Card>
  </div>
</template>
