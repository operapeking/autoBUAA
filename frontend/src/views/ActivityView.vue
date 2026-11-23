<script setup lang="ts">
import { ref } from 'vue';
import { api, type ScheduleItem, type ExecutionItem, type NotificationEvent } from '@/api/client';
import { taskNames, notificationStatusNames } from '@/lib/format';
import { useLive } from '@/composables/useLive';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import ActivityRecord from '@/components/ActivityRecord.vue';

const schedules = ref<ScheduleItem[]>([]);
const executions = ref<ExecutionItem[]>([]);
const events = ref<NotificationEvent[]>([]);
const { error } = useLive(async () => {
  const [s, e, n] = await Promise.all([api.tasks.schedules(), api.tasks.executions(), api.notifications.events()]);
  schedules.value = s.filter(row => ['enabled', 'paused'].includes(row.status));
  executions.value = e;
  events.value = n;
});

function executionTimes(execution: ExecutionItem) {
  return [
    { label: '计划执行', value: execution.scheduled_for },
    { label: '开始执行', value: execution.started_at },
    { label: '执行结束', value: execution.finished_at },
    { label: '下次重试', value: ['pending', 'waiting', 'pending_confirm'].includes(execution.status) ? execution.resume_at || execution.next_retry_at : null },
  ];
}

function notificationTimes(event: NotificationEvent) {
  const delivery = event.deliveries?.filter(item => item.status === 'succeeded').at(-1);
  return [
    { label: '创建时间', value: event.created_at },
    { label: '发送时间', value: delivery?.attempted_at },
    { label: '下次发送', value: event.status === 'pending' ? event.next_retry_at : null },
  ];
}
</script>

<template>
  <div class="space-y-7">
    <header><h1>执行记录</h1></header>
    <p v-if="error" role="alert" class="error-box">{{ error }}</p>
    <Card>
      <CardHeader><CardTitle>任务计划</CardTitle></CardHeader>
      <CardContent>
        <p v-if="!schedules.length" class="empty-state">暂无计划</p>
        <ActivityRecord v-for="schedule in schedules" :key="schedule.id"
          :title="schedule.name" :module="schedule.module" :status="schedule.status"
          :description="schedule.blocking_reason"
          :times="[{ label: '下次执行', value: schedule.status === 'enabled' ? schedule.next_run_at : null }]"
        />
      </CardContent>
    </Card>
    <Tabs default-value="executions">
      <TabsList><TabsTrigger value="executions">任务记录</TabsTrigger><TabsTrigger value="notifications">通知记录</TabsTrigger></TabsList>
      <TabsContent value="executions" class="mt-5">
        <Card><CardContent class="pt-2">
          <p v-if="!executions.length" class="empty-state">暂无执行记录</p>
          <ActivityRecord v-for="execution in executions" :key="execution.id"
            :title="taskNames[execution.task_type] || '自动任务'" :module="execution.module" :status="execution.status"
            :description="execution.result_summary || execution.wait_reason || execution.error_message"
            :times="executionTimes(execution)"
          />
        </CardContent></Card>
      </TabsContent>
      <TabsContent value="notifications" class="mt-5">
        <Card><CardContent class="pt-2">
          <p v-if="!events.length" class="empty-state">暂无通知</p>
          <ActivityRecord v-for="event in events" :key="event.id"
            :title="event.title" :module="event.module" :status="event.status" :status-text="notificationStatusNames[event.status]"
            :description="event.body.replace(/\s*\|\s*/g, '\n')" :times="notificationTimes(event)"
          />
        </CardContent></Card>
      </TabsContent>
    </Tabs>
  </div>
</template>
