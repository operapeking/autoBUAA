export class ApiError extends Error {}
export interface AuthStatus { initialized: boolean; authenticated: boolean }
export type NetworkMode = 'direct' | 'webvpn' | 'vpn_buaa';
export type CampusOption = '北京' | '学院路' | '沙河' | '杭州';
export interface ModuleConfigItem { module: string; enabled: boolean; config_json: Record<string, unknown>; health_status: string; last_error: string | null; last_checked_at: string | null }
export interface AccountInfo { student_id: string; campus: CampusOption; enabled: boolean; has_password: boolean; auth_status: string; auth_error: string | null; has_captcha_image: boolean; updated_at: string }
export interface ClassroomCourse { course_sched_id: string; course_name: string; class_begin_time: string; class_end_time: string; signed: boolean }
export interface BoyaCourse { cancel_end?: string | null; is_ended?: boolean; reservation_status?: string | null; resolved_campus?: string; course_id: number; name: string; location: string; sub_category: string; sign_method: string; has_autonomous_sign: boolean; selected: boolean; chosen_id: number | null; course_start: string | null; select_start: string | null; select_end: string | null; current_count: number; max_count: number | null; signin_done: boolean; signout_done: boolean; checkin: number | null; pass_status: number | null }
export interface BoyaData { courses: BoyaCourse[]; statistics: { valid_count?: number; categories?: { sub_category: string; passed_count: number; required_count: number }[] } }
export interface YgdkData { synced: boolean; synced_at: string | null; overview: { term_name?: string; term_count: number | null; term_target: number | null; week_count: number | null; week_target: number | null } | null }
export interface TdData { synced: boolean; synced_at: string | null; overview: { academic_year: string; semester: number; term_name: string; term_count: number | null; term_target: number; attendance_count: number | null; activity_count: number | null; running_count: number | null } | null }
export interface ScheduleItem { id: number; name: string; module: string; status: string; next_run_at: string | null; last_result_summary: string | null; blocking_reason: string | null }
export interface ExecutionItem { started_at?: string | null; finished_at?: string | null; next_retry_at?: string | null; resume_at?: string | null; wait_reason?: string | null; error_message?: string | null; id: number; module: string; task_type: string; status: string; scheduled_for: string; result_summary: string | null }
export interface NotificationEvent { module?: string; deliveries?: { status: string; attempted_at: string | null; error_message?: string | null }[]; id: number; title: string; body: string; status: string; created_at: string; next_retry_at: string | null }
export interface NotificationRule { id: number; event_type: string; module: string; enabled: boolean; advance_minutes: number; description: string }
export interface NotificationChannel { id: number; name: string; enabled: boolean; server_url: string; has_device_key: boolean; extra: { group?: string; sound?: string } }
let onUnauthorized: (() => void) | undefined;
export function setUnauthorizedHandler(handler: () => void) { onUnauthorized = handler; }
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api/v1' + path, { ...init, credentials: 'include', headers: { 'Content-Type': 'application/json', ...init.headers }, signal: AbortSignal.timeout(20000) });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith('/auth')) onUnauthorized?.();
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((e: { msg: string }) => e.msg).join('；') : detail?.message || `请求失败 (${response.status})`;
    throw new ApiError(message);
  }
  return data as T;
}
const put = <T>(path: string, payload: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(payload) });
const post = <T = Record<string, unknown>>(path: string, payload?: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(payload ?? {}) });
export const api = {
  auth: { status: () => request<AuthStatus>('/auth/status'), login: (password: string) => post('/auth/login', { password }), setup: (password: string) => post('/auth/setup', { password }), logout: () => post('/auth/logout'), changePassword: (old_password: string, new_password: string) => post('/auth/change-password', { old_password, new_password }) },
  account: { get: () => request<AccountInfo>('/account'), update: (payload: { student_id: string; campus: CampusOption; password?: string; enabled: boolean }) => put<AccountInfo>('/account', payload), login: () => post<{ ok: boolean; message?: string }>('/account/login'), submitCaptcha: (code: string) => post<{ ok: boolean; message?: string }>('/account/captcha', { code }) },
  network: { get: () => request<{ network_mode: NetworkMode; modules: ModuleConfigItem[] }>('/network'), setMode: (network_mode: NetworkMode) => put('/network/mode', { network_mode }), updateModule: (module: string, payload: { enabled: boolean; config_json: Record<string, unknown> }) => put<ModuleConfigItem>(`/network/modules/${module}`, payload) },
  classroom: { courses: () => request<{ courses: ClassroomCourse[] }>('/classroom/courses'), signin: (id: string) => post<{ ok: boolean; message: string }>(`/classroom/signin/${encodeURIComponent(id)}`) },
  boya: { courses: () => request<BoyaData>('/boya/courses'), reserve: (id: number) => post(`/boya/reserve/${id}`), cancelReservation: (id: number) => post(`/boya/reserve/${id}/cancel`), select: (id: number) => post(`/boya/select/${id}`), drop: (id: number) => post(`/boya/drop/${id}`), sign: (id: number, type: 1 | 2) => post(`/boya/sign/${id}?sign_type=${type}`) },
  ygdk: { overview: () => request<YgdkData>('/ygdk/overview') },
  td: { overview: () => request<TdData>('/td/overview'), sync: () => post('/td/sync') },
  tasks: { schedules: () => request<ScheduleItem[]>('/tasks/schedules'), executions: () => request<ExecutionItem[]>('/tasks/executions?limit=40') },
  notifications: { channels: () => request<NotificationChannel[]>('/notifications/channels'), updateChannel: (id: number, payload: unknown) => put(`/notifications/channels/${id}`, payload), test: (id: number) => post<{ ok: boolean; message?: string }>(`/notifications/channels/${id}/test`), rules: () => request<NotificationRule[]>('/notifications/rules'), updateRule: (id: number, payload: { enabled: boolean; advance_minutes: number }) => put(`/notifications/rules/${id}`, payload), events: () => request<NotificationEvent[]>('/notifications/events?limit=40') },
};
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
