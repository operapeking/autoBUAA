import { onMounted, onUnmounted, ref } from 'vue';
export function useLive(load: () => Promise<void>, seconds = 10) {
  const error = ref('');
  const loading = ref(true);
  let active = true;
  let running = false;
  let queued = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function refresh() {
    if (!active || document.hidden) return;
    if (running) { queued = true; return; }
    running = true;
    clearTimeout(timer);
    try { await load(); error.value = ''; }
    catch (err) { error.value = err instanceof Error ? err.message : '获取状态失败'; }
    finally {
      loading.value = false; running = false;
      if (active && queued) { queued = false; void refresh(); }
      else if (active) timer = setTimeout(refresh, seconds * 1000);
    }
  }
  const wake = () => { void refresh(); };
  onMounted(() => { void refresh(); document.addEventListener('visibilitychange', wake); window.addEventListener('focus', wake); });
  onUnmounted(() => { active = false; clearTimeout(timer); document.removeEventListener('visibilitychange', wake); window.removeEventListener('focus', wake); });
  return { error, loading, refresh };
}
