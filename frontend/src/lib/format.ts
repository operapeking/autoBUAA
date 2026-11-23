import type { NetworkMode } from '@/api/client';

export function formatTime(value: string | null | undefined) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}
export const moduleNames: Record<string, string> = { classroom: '课堂签到', boya: '博雅课程', ygdk: '阳光打卡', td: '体能锻炼', auth: '统一认证', system: '系统' };
export const statusNames: Record<string, string> = { enabled: '运行中', paused: '已暂停', completed: '已完成', cancelled: '已取消', pending: '等待执行', running: '执行中', waiting: '等待处理', pending_confirm: '等待确认', succeeded: '成功', failed: '失败', expired: '已过期', sent: '已发送', skipped: '已跳过' };
export const networkNames: Record<NetworkMode, string> = { direct: '校园网直连', webvpn: '校外访问（d.buaa.edu.cn）', vpn_buaa: '校外访问（vpn.buaa.edu.cn）' };
export const taskNames: Record<string, string> = { 'classroom.sync_today': '课堂课程更新', 'classroom.auto_signin': '课堂自动签到', 'boya.sync_courses': '博雅课程更新', 'boya.auto_select': '博雅预约报名', 'boya.auto_signin': '博雅自动签到', 'boya.auto_signout': '博雅自动签退', 'ygdk.sync_overview': '阳光打卡进度更新', 'td.sync_overview': '体能锻炼状态更新' };
export const notificationStatusNames: Record<string, string> = { pending: '等待发送', sent: '已发送', failed: '发送失败', expired: '已过期', skipped: '已跳过' };
export function formatRecordTime(value: string | null | undefined) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date);
}
