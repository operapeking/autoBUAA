<script setup lang="ts">
import { ref } from 'vue';
import { useIntervalFn } from '@vueuse/core';
import type { BoyaCourse } from '@/api/client';
import { formatTime } from '@/lib/format';
import { attendanceStatus, assessmentStatus } from '@/lib/boya';
import BoyaCourseDetails from './BoyaCourseDetails.vue';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
defineProps<{ courses: BoyaCourse[]; busy: boolean }>();
const emit = defineEmits<{ sign: [id: number, type: 1 | 2]; drop: [chosenId: number] }>();
const now = ref(Date.now());
useIntervalFn(() => { now.value = Date.now(); }, 1000);
function canDrop(course: BoyaCourse, time = now.value) {
  return course.selected && course.chosen_id !== null && course.chosen_id > 0
    && !!course.cancel_end && time < new Date(course.cancel_end).getTime();
}
function drop(course: BoyaCourse) {
  if (canDrop(course, Date.now()) && course.chosen_id !== null) emit('drop', course.chosen_id);
}
</script>

<template>
  <Card aria-labelledby="boya-selected-title">
    <CardHeader><div class="flex items-center justify-between gap-3"><CardTitle id="boya-selected-title">已选课程</CardTitle><span class="text-sm text-muted-foreground">{{ courses.length }} 门</span></div></CardHeader>
    <CardContent>
      <p v-if="!courses.length" class="empty-state">暂无已选课程</p>
      <article v-for="course in courses" :key="course.course_id" class="border-b last:border-b-0 py-5 first:pt-0 last:pb-0 space-y-4">
        <div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(240px,auto)]">
          <BoyaCourseDetails :course="course" />
          <dl class="grid grid-cols-2 gap-x-6 gap-y-3 rounded-lg bg-muted/60 p-4 text-sm self-start">
            <div><dt class="text-xs text-muted-foreground">考勤</dt><dd class="mt-1 font-medium" :class="course.checkin === 1 ? 'text-primary' : course.checkin !== null && course.checkin > 1 ? 'text-destructive' : ''">{{ attendanceStatus(course.checkin) }}</dd></div>
            <div><dt class="text-xs text-muted-foreground">考核</dt><dd class="mt-1 font-medium" :class="course.pass_status === 1 ? 'text-primary' : course.pass_status === 0 ? 'text-destructive' : ''">{{ assessmentStatus(course.pass_status) }}</dd></div>
            <template v-if="course.has_autonomous_sign">
              <div><dt class="text-xs text-muted-foreground">签到</dt><dd class="mt-1">{{ course.signin_done ? '已完成' : '待签到' }}</dd></div>
              <div><dt class="text-xs text-muted-foreground">签退</dt><dd class="mt-1">{{ course.signout_done ? '已完成' : '待签退' }}</dd></div>
            </template>
          </dl>
        </div>
        <div v-if="canDrop(course) || (course.has_autonomous_sign && (!course.signin_done || !course.signout_done))" class="flex flex-wrap items-center justify-end gap-2">
          <template v-if="canDrop(course)">
            <span class="text-xs text-muted-foreground mr-auto">退选截止 {{ formatTime(course.cancel_end) }}</span>
            <Button variant="outline" size="sm" class="text-destructive" :disabled="busy" @click="drop(course)">退选</Button>
          </template>
          <Button v-if="course.has_autonomous_sign && !course.signin_done" variant="outline" size="sm" :disabled="busy" @click="$emit('sign', course.course_id, 1)">签到</Button>
          <Button v-if="course.has_autonomous_sign && !course.signout_done" variant="outline" size="sm" :disabled="busy" @click="$emit('sign', course.course_id, 2)">签退</Button>
        </div>
      </article>
    </CardContent>
  </Card>
</template>
