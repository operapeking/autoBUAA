import type { BoyaCourse } from '@/api/client';

function startTime(course: BoyaCourse) {
  const time = course.course_start ? new Date(course.course_start).getTime() : NaN;
  return Number.isNaN(time) ? Infinity : time;
}

export function sortBoyaCourses(courses: BoyaCourse[]) {
  return [...courses].sort((a, b) => (startTime(a) - startTime(b)) || a.course_id - b.course_id);
}

export function formatBoyaStart(value: string | null) {
  if (!value) return '待公布';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '待公布';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    weekday: 'long', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

export function attendanceStatus(value: number | null) {
  const labels: Record<number, string> = {
    0: '待考勤', 1: '通过', 2: '迟到', 3: '早退', 4: '缺勤',
    5: '已签到、待签退', 6: '签到异常、待签退', 7: '待签到、已签退',
    8: '待签到、签退异常', 9: '签到异常、已签退', 10: '已签到、签退异常',
    11: '签到异常、签退异常',
  };
  return value === null ? '暂无结果' : labels[value] ?? `状态 ${value}`;
}

export function assessmentStatus(value: number | null) {
  return value === null ? '待考核' : value === 1 ? '通过' : value === 0 ? '未通过' : `状态 ${value}`;
}
