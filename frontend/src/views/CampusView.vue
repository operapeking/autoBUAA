<script setup lang="ts">
import { computed, ref } from 'vue';
import { api, type ModuleConfigItem, type ClassroomCourse, type BoyaData, type YgdkData, type TdData } from '@/api/client';
import { formatTime, moduleNames } from '@/lib/format';
import { useLive } from '@/composables/useLive';
import { sortBoyaCourses } from '@/lib/boya';
import BoyaSelectedCourses from '@/components/BoyaSelectedCourses.vue';
import BoyaCourseSelection from '@/components/BoyaCourseSelection.vue';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
const modules = ref<ModuleConfigItem[]>([]);
const classroom = ref<ClassroomCourse[]>([]);
const boya = ref<BoyaData>({ courses: [], statistics: {} });
const sunshine = ref<YgdkData | null>(null);
const fitness = ref<TdData | null>(null);
const busy = ref(false);
const actionError = ref('');
const selectedCourses = computed(() => sortBoyaCourses(boya.value.courses.filter(course => course.selected)));
const availableCourses = computed(() => sortBoyaCourses(boya.value.courses.filter(course => !course.selected)));
const { error, loading, refresh } = useLive(async () => {
  if (busy.value) return;
  const [mods, cls, by, yg, td] = await Promise.all([api.modules.list(), api.classroom.courses(), api.boya.courses(), api.ygdk.overview(), api.td.overview()]);
  modules.value = mods; classroom.value = cls.courses; boya.value = by; sunshine.value = yg; fitness.value = td;
});
async function act(fn: () => Promise<unknown>) {
  if (busy.value) return;
  busy.value = true; actionError.value = '';
  try { await fn(); } catch (err) { actionError.value = err instanceof Error ? err.message : '操作失败'; }
  finally { busy.value = false; await refresh(); }
}
function saveModule(mod: ModuleConfigItem, changes: { enabled?: boolean; config?: Record<string, unknown> }) {
  return act(() => api.modules.update(mod.module, { enabled: changes.enabled ?? mod.enabled, config_json: { ...mod.config_json, ...changes.config } }));
}
function progress(count: number | null | undefined, target: number | null | undefined) { return `${count ?? '—'} / ${target ?? '—'}`; }
</script>
<template>
  <div class="space-y-7">
    <header><h1>校园事务</h1></header>
    <p v-if="error || actionError" role="alert" class="error-box">{{ actionError || error }}</p>
    <p v-if="loading" class="text-muted-foreground">正在获取校园事务…</p>
    <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
      <Card v-for="mod in modules" :key="mod.module">
        <CardHeader><div class="flex justify-between items-center"><CardTitle>{{ moduleNames[mod.module] }}</CardTitle><Switch :aria-label="`启用${moduleNames[mod.module]}`" :model-value="mod.enabled" :disabled="busy" @update:model-value="value => saveModule(mod, { enabled: value })" /></div></CardHeader>
        <CardContent class="space-y-4">
          <div v-if="mod.module === 'classroom' || mod.module === 'boya'" class="flex items-center justify-between text-sm"><span>自动签到{{ mod.module === 'boya' ? '与签退' : '' }}</span><Switch :aria-label="`${moduleNames[mod.module]}自动签到`" :model-value="mod.config_json.auto_sign !== false" :disabled="busy || !mod.enabled" @update:model-value="value => saveModule(mod, { config: { auto_sign: value } })" /></div>
          <div v-else-if="mod.module === 'ygdk'" class="flex items-center justify-between text-sm"><label for="reminder-time">每日提醒时间</label><input id="reminder-time" class="compact-input" type="time" :value="mod.config_json.reminder_time" :disabled="busy" @change="event => saveModule(mod, { config: { reminder_time: (event.target as HTMLInputElement).value } })" /></div>
          <template v-else-if="mod.module === 'td'">
            <div class="flex items-center justify-between text-sm"><label for="td-term-target">学期目标次数</label><input id="td-term-target" class="compact-input w-20" type="number" min="1" max="10000" step="1" :value="mod.config_json.term_target" :disabled="busy" @change="event => saveModule(mod, { config: { term_target: Number((event.target as HTMLInputElement).value) } })" /></div>
            <p class="text-sm text-muted-foreground">启用后每小时更新状态</p>
          </template>
          <div v-if="mod.module === 'boya'" class="flex items-center justify-between text-sm"><span>按照账号校区过滤</span><Switch aria-label="博雅课程按照校区过滤" :model-value="mod.config_json.filter_by_campus !== false" :disabled="busy" @update:model-value="value => saveModule(mod, { config: { filter_by_campus: value } })" /></div>
          <p v-if="mod.last_error" class="text-sm text-destructive">{{ mod.last_error }}</p>
          <p class="text-xs text-muted-foreground">{{ mod.enabled ? '已启用' : '已停用' }} · 更新于 {{ formatTime(mod.last_checked_at) }}</p>
        </CardContent>
      </Card>
    </div>
    <Tabs default-value="classroom">
      <TabsList><TabsTrigger value="classroom">今日课堂</TabsTrigger><TabsTrigger value="boya">博雅课程</TabsTrigger><TabsTrigger value="ygdk">阳光打卡</TabsTrigger><TabsTrigger value="td">体能锻炼</TabsTrigger></TabsList>
      <TabsContent value="classroom" class="mt-5">
        <Card><CardHeader><CardTitle>今日课程</CardTitle></CardHeader><CardContent>
          <p v-if="!classroom.length" class="empty-state">今日暂无课程</p>
          <div v-for="course in classroom" :key="course.course_sched_id" class="data-row">
            <div><p class="font-medium">{{ course.course_name }}</p><p class="text-sm text-muted-foreground mt-1">{{ course.class_begin_time }} — {{ course.class_end_time }}</p></div>
            <Badge v-if="course.signed" variant="secondary">已签到</Badge>
            <Button v-else variant="outline" size="sm" :disabled="busy" @click="act(async () => { const result = await api.classroom.signin(course.course_sched_id); if (!result.ok) throw new Error(result.message); })">签到</Button>
          </div>
        </CardContent></Card>
      </TabsContent>
      <TabsContent value="boya" class="mt-5 space-y-5">
        <div v-if="boya.statistics.categories?.length" class="grid gap-3 sm:grid-cols-4"><Card v-for="category in boya.statistics.categories" :key="category.sub_category"><CardContent class="pt-5"><p class="text-sm text-muted-foreground">{{ category.sub_category }}</p><p class="text-2xl font-semibold mt-2">{{ progress(category.passed_count, category.required_count) }}</p></CardContent></Card></div>
        <BoyaSelectedCourses :courses="selectedCourses" :busy="busy" @sign="(id, type) => act(() => api.boya.sign(id, type))" @drop="chosenId => act(() => api.boya.drop(chosenId))" />
        <BoyaCourseSelection :courses="availableCourses" :busy="busy" @reserve="id => act(() => api.boya.reserve(id))" @cancel-reservation="id => act(() => api.boya.cancelReservation(id))" @select="id => act(() => api.boya.select(id))" />
      </TabsContent>
      <TabsContent value="ygdk" class="mt-5"><Card><CardHeader><CardTitle>{{ sunshine?.overview?.term_name || '运动打卡进度' }}</CardTitle></CardHeader><CardContent>
        <div v-if="sunshine?.overview" class="grid grid-cols-2 gap-8 py-5"><div><p class="text-sm text-muted-foreground">本学期</p><p class="text-4xl font-semibold mt-3">{{ progress(sunshine.overview.term_count, sunshine.overview.term_target) }}</p></div><div><p class="text-sm text-muted-foreground">本周</p><p class="text-4xl font-semibold mt-3">{{ progress(sunshine.overview.week_count, sunshine.overview.week_target) }}</p></div></div>
        <p v-else class="empty-state">等待每日进度更新</p><p class="text-xs text-muted-foreground mt-4">更新于 {{ formatTime(sunshine?.synced_at) }}</p>
      </CardContent></Card></TabsContent>
      <TabsContent value="td" class="mt-5">
        <Card>
          <CardHeader><div class="flex items-center justify-between gap-4"><CardTitle>{{ fitness?.overview?.term_name || '体能锻炼进度' }}</CardTitle><Button variant="outline" size="sm" :disabled="busy" @click="act(() => api.td.sync())">刷新状态</Button></div></CardHeader>
          <CardContent>
            <template v-if="fitness?.overview">
              <div class="flex items-center gap-4 py-5"><div><p class="text-sm text-muted-foreground">本学期 TD 合计</p><p class="text-4xl font-semibold mt-3">{{ progress(fitness.overview.term_count, fitness.overview.term_target) }}</p></div><Badge v-if="fitness.overview.term_count != null && fitness.overview.term_count >= fitness.overview.term_target" variant="secondary">已达标</Badge></div>
              <div class="grid grid-cols-3 gap-4 py-5 text-sm"><div><p class="text-muted-foreground">TD 考勤机</p><p class="text-xl font-semibold mt-2">{{ fitness.overview.attendance_count ?? '—' }}</p></div><div><p class="text-muted-foreground">活动转换</p><p class="text-xl font-semibold mt-2">{{ fitness.overview.activity_count ?? '—' }}</p></div><div><p class="text-muted-foreground">奔跑在北航</p><p class="text-xl font-semibold mt-2">{{ fitness.overview.running_count ?? '—' }}</p></div></div>
              <p v-if="fitness.overview.term_count == null" class="empty-state">当前学期暂无锻炼次数</p>
            </template>
            <p v-else class="empty-state">等待体能锻炼状态更新</p>
            <p class="text-xs text-muted-foreground mt-4">更新于 {{ formatTime(fitness?.synced_at) }}</p>
          </CardContent>
        </Card>
      </TabsContent>
    </Tabs>
  </div>
</template>
