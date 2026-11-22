<script setup lang="ts">
import { reactive, ref } from 'vue';
import { api, type NotificationRule } from '@/api/client';
import { useLive } from '@/composables/useLive';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
const channelId = ref<number | null>(null), hasKey = ref(false), busy = ref(false), message = ref(''), error = ref('');
const form = reactive({ enabled: false, server_url: 'https://api.day.app', device_key: '', group: 'autoBUAA' });
const rules = ref<NotificationRule[]>([]);
let initialized = false;
const { error: loadError } = useLive(async () => {
  if (initialized) return;
  const [channels, r] = await Promise.all([api.notifications.channels(), api.notifications.rules()]);
  const channel = channels[0];
  if (channel) { channelId.value = channel.id; form.enabled = channel.enabled; form.server_url = channel.server_url; form.group = channel.extra.group || 'autoBUAA'; hasKey.value = channel.has_device_key; }
  rules.value = r; initialized = true;
});
async function act(fn: () => Promise<void>) { if (busy.value) return; busy.value = true; message.value = ''; error.value = ''; try { await fn(); } catch (err) { error.value = err instanceof Error ? err.message : '保存失败'; } finally { busy.value = false; } }
async function save() {
  if (channelId.value === null) return;
  await api.notifications.updateChannel(channelId.value, { ...form, device_key: form.device_key || undefined, name: 'Bark' });
  if (form.device_key) hasKey.value = true;
  form.device_key = ''; message.value = '通知设置已保存';
}
</script>
<template>
  <Card><CardHeader><CardTitle>Bark 通知</CardTitle></CardHeader><CardContent class="space-y-5">
    <p v-if="error || loadError" role="alert" class="error-box">{{ error || loadError }}</p><p v-if="message" role="status" class="success-box">{{ message }}</p>
    <form class="space-y-5" @submit.prevent="act(save)"><div class="flex items-center gap-3"><Switch id="bark-enabled" v-model="form.enabled" /><label for="bark-enabled" class="text-sm">启用推送</label></div><div class="grid gap-5 sm:grid-cols-2"><div class="field"><label for="bark-url">服务器地址</label><Input id="bark-url" v-model="form.server_url" type="url" required /></div><div class="field"><label for="bark-key">设备密钥</label><Input id="bark-key" v-model="form.device_key" type="password" autocomplete="off" :required="form.enabled && !hasKey" :placeholder="hasKey ? '留空保留已保存密钥' : ''" /></div></div><div class="flex gap-3"><Button type="submit" :disabled="busy || channelId === null">保存通知设置</Button><Button variant="outline" type="button" :disabled="busy || channelId === null" @click="act(async () => { await save(); const result = await api.notifications.test(channelId!); if (!result.ok) throw new Error(result.message || '推送失败'); message = '测试通知已发送'; })">测试推送</Button></div></form>
    <div class="border-t pt-3"><div v-for="rule in rules" :key="rule.id" class="data-row gap-4"><div class="flex-1"><label :for="`rule-${rule.id}`" class="text-sm">{{ rule.description || rule.event_type }}</label></div><div v-if="['signup_reminder', 'signin_reminder', 'signout_reminder'].includes(rule.event_type)" class="flex items-center gap-2 text-sm"><Input :model-value="rule.advance_minutes" type="number" min="0" max="180" :aria-label="`${rule.description}提前分钟`" class="w-20" :disabled="busy" @change="(event: Event) => act(async () => { const value = Number((event.target as HTMLInputElement).value); await api.notifications.updateRule(rule.id, { enabled: rule.enabled, advance_minutes: value }); rule.advance_minutes = value; })" /><span class="text-muted-foreground">分钟前</span></div><Switch :id="`rule-${rule.id}`" :model-value="rule.enabled" :disabled="busy" @update:model-value="value => act(async () => { await api.notifications.updateRule(rule.id, { enabled: value, advance_minutes: rule.advance_minutes }); rule.enabled = value; })" /></div></div>
  </CardContent></Card>
</template>
