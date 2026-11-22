<script setup lang="ts">
import type { BoyaCourse } from '@/api/client';
import { formatBoyaStart } from '@/lib/boya';
defineProps<{ course: BoyaCourse }>();
</script>

<template>
  <div class="min-w-0 space-y-2">
    <p class="text-xs text-muted-foreground">{{ course.sub_category || '博雅课程' }}</p>
    <h4 class="font-semibold leading-relaxed">{{ course.name }}</h4>
    <p class="text-sm font-medium">开课 {{ formatBoyaStart(course.course_start) }}</p>
    <p class="text-sm text-muted-foreground">{{ [course.resolved_campus, course.location].filter((value, index, values) => value && values.indexOf(value) === index).join(' · ') || '地点待公布' }}</p>
    <p class="text-xs text-muted-foreground">{{ course.has_autonomous_sign ? '自主签到' : '常规签到' }}<span v-if="course.is_ended"> · 课程已结束</span></p>
  </div>
</template>
