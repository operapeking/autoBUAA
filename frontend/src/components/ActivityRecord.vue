<script setup lang="ts">
import { Badge } from '@/components/ui/badge';
import { formatRecordTime, moduleNames, statusNames } from '@/api/client';

defineProps<{
  title: string;
  module?: string;
  status: string;
  statusText?: string;
  description?: string | null;
  times: { label: string; value: string | null | undefined }[];
}>();
</script>

<template>
  <article class="data-row items-start gap-4">
    <div class="min-w-0 flex-1 space-y-2">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="font-medium break-words">{{ title }}</h3>
        <Badge v-if="moduleNames[module || '']" variant="outline">{{ moduleNames[module || ''] }}</Badge>
      </div>
      <p v-if="description" class="text-sm text-muted-foreground whitespace-pre-line break-words leading-6">{{ description }}</p>
      <dl v-if="times.some(item => item.value)" class="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground tabular-nums">
        <div v-for="time in times.filter(item => item.value)" :key="time.label" class="flex flex-wrap gap-x-2">
          <dt>{{ time.label }}</dt><dd><time :datetime="time.value!">{{ formatRecordTime(time.value) }}</time></dd>
        </div>
      </dl>
    </div>
    <Badge class="mt-0.5" :variant="status === 'failed' ? 'destructive' : ['succeeded', 'sent', 'completed'].includes(status) ? 'default' : 'secondary'">{{ statusText || statusNames[status] || status }}</Badge>
  </article>
</template>
