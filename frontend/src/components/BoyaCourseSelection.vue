<script setup lang="ts">
import type { BoyaCourse } from '@/api/client';
import { formatTime } from '@/lib/format';
import BoyaCourseDetails from './BoyaCourseDetails.vue';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
defineProps<{ courses: BoyaCourse[]; busy: boolean }>();
defineEmits<{ reserve: [id: number]; cancelReservation: [id: number]; select: [id: number] }>();
</script>

<template>
  <Card aria-labelledby="boya-selection-title">
    <CardHeader><div class="flex items-center justify-between gap-3"><CardTitle id="boya-selection-title">选择课程</CardTitle><span class="text-sm text-muted-foreground">{{ courses.length }} 门</span></div></CardHeader>
    <CardContent>
      <p v-if="!courses.length" class="empty-state">暂无可选课程</p>
      <article v-for="course in courses" :key="course.course_id" class="border-b last:border-b-0 py-5 first:pt-0 last:pb-0 space-y-4">
        <BoyaCourseDetails :course="course" />
        <div class="flex flex-wrap items-end justify-between gap-4">
          <div class="text-xs text-muted-foreground space-y-1">
            <p>报名时间 {{ formatTime(course.select_start) }} — {{ formatTime(course.select_end) }}</p>
            <p>报名人数 {{ course.current_count }} / {{ course.max_count ?? '不限' }}<span v-if="course.max_count !== null && course.current_count >= course.max_count"> · 人数已满</span></p>
            <p v-if="['enabled', 'paused'].includes(course.reservation_status || '')" class="text-primary">{{ course.reservation_status === 'paused' ? '预约已暂停' : '已预约报名' }}</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <Button v-if="['enabled', 'paused'].includes(course.reservation_status || '')" variant="outline" size="sm" :disabled="busy" @click="$emit('cancelReservation', course.course_id)">取消预约</Button>
            <Button v-else variant="outline" size="sm" :disabled="busy || !course.select_start || !course.select_end || Date.now() >= new Date(course.select_end).getTime()" @click="$emit('reserve', course.course_id)">预约报名</Button>
            <Button size="sm" :disabled="busy || !course.select_start || !course.select_end || Date.now() < new Date(course.select_start).getTime() || Date.now() >= new Date(course.select_end).getTime() || (course.max_count !== null && course.current_count >= course.max_count)" @click="$emit('select', course.course_id)">报名</Button>
          </div>
        </div>
      </article>
    </CardContent>
  </Card>
</template>
