import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, type Component } from 'vue';
import { setImmediate } from 'node:timers';
import { api, type AccountInfo, type ModuleConfigItem, type BoyaCourse } from '@/api/client';
import ModulesView from '@/views/ModulesView.vue';
import AccountNetworkView from '@/views/AccountNetworkView.vue';
import TasksView from '@/views/TasksView.vue';

vi.mock('@/api/client', async (original) => {
  const actual = await original<typeof import('@/api/client')>();
  return { ...actual, api: {
    account: { get: vi.fn(), update: vi.fn(), login: vi.fn(), submitCaptcha: vi.fn() },
    network: { get: vi.fn(), updateModule: vi.fn(), setMode: vi.fn() },
    classroom: { courses: vi.fn(), signin: vi.fn() },
    boya: { courses: vi.fn(), drop: vi.fn(), select: vi.fn(), sign: vi.fn(), reserve: vi.fn(), cancelReservation: vi.fn() },
    ygdk: { overview: vi.fn() },
    td: { overview: vi.fn(), sync: vi.fn() },
    tasks: { schedules: vi.fn(), executions: vi.fn() },
    notifications: { events: vi.fn(), channels: vi.fn(), rules: vi.fn(), updateChannel: vi.fn(), test: vi.fn() },
    auth: { changePassword: vi.fn() },
  } };
});

const modules: ModuleConfigItem[] = [
  { module: 'classroom', enabled: false, config_json: { auto_sign: true, signin_offset_minutes: -5 }, health_status: 'unknown', last_error: null, last_checked_at: null },
  { module: 'boya', enabled: false, config_json: { auto_sign: true }, health_status: 'unknown', last_error: null, last_checked_at: null },
  { module: 'ygdk', enabled: false, config_json: { reminder_time: '18:00' }, health_status: 'unknown', last_error: null, last_checked_at: null },
  { module: 'td', enabled: false, config_json: { term_target: 32 }, health_status: 'unknown', last_error: null, last_checked_at: null },
];
const account: AccountInfo = { student_id: '23000001', campus: '沙河', enabled: true, has_password: true, auth_status: 'configured', auth_error: null, has_captcha_image: false, updated_at: '' };
const cleanups: (() => void)[] = [];
const flush = () => new Promise<void>(resolve => setImmediate(resolve));
function render(component: Component) {
  const element = document.createElement('div'); document.body.append(element);
  const app = createApp(component); app.mount(element);
  const dispose = () => { app.unmount(); element.remove(); };
  cleanups.push(dispose);
  return { element, dispose };
}
async function input(element: HTMLElement, id: string, value: string) {
  const field = element.querySelector<HTMLInputElement>(id)!;
  field.value = value; field.dispatchEvent(new Event('input', { bubbles: true })); await nextTick();
}
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks();
  vi.mocked(api.network.get).mockResolvedValue({ network_mode: 'webvpn', modules: structuredClone(modules) });
  vi.mocked(api.classroom.courses).mockResolvedValue({ courses: [] });
  vi.mocked(api.boya.courses).mockResolvedValue({ courses: [], statistics: {} });
  vi.mocked(api.ygdk.overview).mockResolvedValue({ synced: false, synced_at: null, overview: null });
  vi.mocked(api.td.overview).mockResolvedValue({ synced: false, synced_at: null, overview: null });
  vi.mocked(api.account.get).mockResolvedValue(account);
  vi.mocked(api.notifications.channels).mockResolvedValue([{ id: 1, name: 'Bark', enabled: false, server_url: 'https://api.day.app', has_device_key: true, extra: {} }]);
  vi.mocked(api.notifications.rules).mockResolvedValue([]);
});
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); vi.useRealTimers(); });

describe('校园事务', () => {
  it('显示体能锻炼次数并刷新达标状态', async () => {
    const overview = { academic_year: '2026-2027', semester: 1, term_name: '2026-2027 学年第 1 学期', term_count: 31, term_target: 32, attendance_count: 30, activity_count: 1, running_count: 0 };
    vi.mocked(api.td.overview).mockResolvedValue({ synced: true, synced_at: '2026-11-16T10:00:00+08:00', overview });
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '体能锻炼')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    expect(element.textContent).toContain('31 / 32');
    expect(element.textContent).toContain('TD 考勤机');
    expect(element.textContent).not.toContain('已达标');
    expect(element.querySelector('[aria-label="体能锻炼自动签到"]')).toBeNull();
    vi.mocked(api.td.overview).mockResolvedValue({ synced: true, synced_at: null, overview: { ...overview, term_count: 32 } });
    [...element.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '刷新状态')!.click(); await flush();
    expect(api.td.sync).toHaveBeenCalledOnce();
    expect(element.textContent).toContain('32 / 32');
    expect(element.textContent).toContain('已达标');
  });
  it('体能锻炼查询失败时显示错误并保留缓存次数', async () => {
    vi.mocked(api.td.overview).mockResolvedValue({ synced: true, synced_at: null, overview: { academic_year: '2026-2027', semester: 1, term_name: '秋学期', term_count: 0, term_target: 32, attendance_count: 0, activity_count: 0, running_count: 0 } });
    vi.mocked(api.td.sync).mockRejectedValue(new Error('健康云网络请求失败'));
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '体能锻炼')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    [...element.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '刷新状态')!.click(); await flush();
    expect(element.querySelector('[role="alert"]')!.textContent).toContain('健康云网络请求失败');
    expect(element.textContent).toContain('0 / 32');
  });
  it('退选使用选课记录编号，成功后更新两个课程模块', async () => {
    vi.setSystemTime(new Date('2026-11-16T10:00:00+08:00'));
    const course: BoyaCourse = { course_id: 903, chosen_id: 1903, name: '文化讲座', selected: true, cancel_end: '2026-11-16T11:00:00+08:00', course_start: '2026-11-17T10:00:00+08:00', location: '报告厅', sub_category: '美育', sign_method: '', has_autonomous_sign: false, select_start: null, select_end: null, current_count: 1, max_count: 10, signin_done: false, signout_done: false, checkin: null, pass_status: null };
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [course], statistics: {} });
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '博雅课程')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    const selected = element.querySelector('[aria-labelledby="boya-selected-title"]')!;
    expect(selected.textContent).toContain('退选截止');
    const button = [...selected.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '退选')!;
    expect(button.disabled).toBe(false);
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [{ ...course, selected: false, chosen_id: null }], statistics: {} });
    button.click(); await flush();
    expect(api.boya.drop).toHaveBeenCalledWith(1903);
    expect(selected.textContent).toContain('暂无已选课程');
    expect(element.querySelector('[aria-labelledby="boya-selection-title"]')!.textContent).toContain('文化讲座');
  });
  it('退选截止时移除按钮，缺少有效截止时间或选课记录的课程不显示退选', async () => {
    vi.setSystemTime(new Date('2026-11-16T10:00:00+08:00'));
    const base: BoyaCourse = { course_id: 904, chosen_id: 1904, name: '即将截止', selected: true, cancel_end: '2026-11-16T10:00:01+08:00', course_start: null, location: '', sub_category: '', sign_method: '', has_autonomous_sign: false, select_start: null, select_end: null, current_count: 1, max_count: 10, signin_done: false, signout_done: false, checkin: null, pass_status: null };
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [
      base,
      { ...base, course_id: 905, cancel_end: '2026-11-16T10:00:00+08:00' },
      { ...base, course_id: 906, cancel_end: null },
      { ...base, course_id: 907, cancel_end: 'invalid' },
      { ...base, course_id: 908, chosen_id: null },
    ], statistics: {} });
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '博雅课程')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    const dropButtons = () => [...element.querySelectorAll<HTMLButtonElement>('[aria-labelledby="boya-selected-title"] button')].filter(button => button.textContent === '退选');
    expect(dropButtons()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000); await flush();
    expect(dropButtons()).toHaveLength(0);
    expect(api.boya.drop).not.toHaveBeenCalled();
  });
  it('博雅课程分为两个模块，各自按照开课时间排序并显示星期与考勤考核', async () => {
    const base: BoyaCourse = { course_id: 1, name: '已选较晚课程', location: '报告厅', sub_category: '美育', sign_method: '自主签到', has_autonomous_sign: true, selected: true, chosen_id: 101, course_start: '2026-11-18T10:00:00+08:00', select_start: null, select_end: null, current_count: 1, max_count: 30, signin_done: true, signout_done: false, checkin: 5, pass_status: null };
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [
      base,
      { ...base, course_id: 2, name: '可选较晚课程', selected: false },
      { ...base, course_id: 3, name: '已选时间待公布', course_start: null, checkin: null, pass_status: 0 },
      { ...base, course_id: 4, name: '已选较早课程', course_start: '2026-11-16T10:00:00+08:00', checkin: 1, pass_status: 1, signout_done: true, is_ended: true },
      { ...base, course_id: 5, name: '可选较早课程', selected: false, course_start: '2026-11-16T16:30:00+08:00' },
      { ...base, course_id: 6, name: '可选时间待公布', selected: false, course_start: 'invalid' },
    ], statistics: {} });
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '博雅课程')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    const selected = element.querySelector('[aria-labelledby="boya-selected-title"]')!;
    const available = element.querySelector('[aria-labelledby="boya-selection-title"]')!;
    expect([...selected.querySelectorAll('article h4')].map(heading => heading.textContent)).toEqual(['已选较早课程', '已选较晚课程', '已选时间待公布']);
    expect([...available.querySelectorAll('article h4')].map(heading => heading.textContent)).toEqual(['可选较早课程', '可选较晚课程', '可选时间待公布']);
    expect(selected.textContent).toContain('星期一');
    expect(available.textContent).toContain('星期一 16:30');
    const rows = selected.querySelectorAll('article');
    expect([...rows[0]!.querySelectorAll('dd')].map(cell => cell.textContent)).toEqual(['通过', '通过', '已完成', '已完成']);
    expect([...rows[1]!.querySelectorAll('dd')].map(cell => cell.textContent)).toEqual(['已签到、待签退', '待考核', '已完成', '待签退']);
    expect([...rows[2]!.querySelectorAll('dd')].map(cell => cell.textContent).slice(0, 2)).toEqual(['暂无结果', '未通过']);
    expect(available.querySelector('dl')).toBeNull();
    expect([...selected.querySelectorAll('button')].some(button => button.textContent === '报名')).toBe(false);
    rows[1]!.querySelector<HTMLButtonElement>('button')!.click(); await flush();
    expect(api.boya.sign).toHaveBeenCalledWith(1, 2);
  });
  it('报名后课程移入已选模块', async () => {
    const course: BoyaCourse = { course_id: 902, name: '科学讲座', location: '沙河', sub_category: '科技', sign_method: '', has_autonomous_sign: false, selected: false, chosen_id: null, course_start: '2099-12-01T20:00:00+08:00', select_start: '2020-12-01T18:00:00+08:00', select_end: '2099-12-01T19:00:00+08:00', current_count: 1, max_count: 10, signin_done: false, signout_done: false, checkin: null, pass_status: null };
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [course], statistics: {} });
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '博雅课程')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    expect(element.querySelector('[aria-labelledby="boya-selected-title"]')!.textContent).toContain('暂无已选课程');
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [{ ...course, selected: true, chosen_id: 100 }], statistics: {} });
    [...element.querySelectorAll<HTMLButtonElement>('[aria-labelledby="boya-selection-title"] button')].find(button => button.textContent === '报名')!.click(); await flush();
    expect(api.boya.select).toHaveBeenCalledWith(902);
    expect(element.querySelector('[aria-labelledby="boya-selected-title"]')!.textContent).toContain('科学讲座');
    expect(element.querySelector('[aria-labelledby="boya-selection-title"]')!.textContent).toContain('暂无可选课程');
  });
  it('保存博雅校区过滤设置', async () => {
    const { element } = render(ModulesView); await flush();
    element.querySelector<HTMLButtonElement>('[aria-label="博雅课程按照校区过滤"]')!.click(); await flush();
    expect(api.network.updateModule).toHaveBeenCalledWith('boya', { enabled: false, config_json: { auto_sign: true, filter_by_campus: false } });
  });
  it('预告和满额课程支持预约及取消预约', async () => {
    const course = { course_id: 901, name: '艺术讲座', location: '沙河', sub_category: '美育', sign_method: '', has_autonomous_sign: false, selected: false, chosen_id: null, course_start: '2099-12-01T20:00:00+08:00', select_start: '2099-12-01T18:00:00+08:00', select_end: '2099-12-01T19:00:00+08:00', current_count: 10, max_count: 10, signin_done: false, signout_done: false, checkin: null, pass_status: null };
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [course], statistics: {} });
    const { element } = render(ModulesView); await flush();
    const tab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(button => button.textContent === '博雅课程')!;
    tab.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); tab.click(); await nextTick(); await flush();
    const reserve = [...element.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '预约报名')!;
    expect(reserve.disabled).toBe(false);
    vi.mocked(api.boya.courses).mockResolvedValue({ courses: [{ ...course, reservation_status: 'enabled' }], statistics: {} });
    reserve.click(); await flush();
    expect(api.boya.reserve).toHaveBeenCalledWith(901);
    [...element.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === '取消预约')!.click(); await flush();
    expect(api.boya.cancelReservation).toHaveBeenCalledWith(901);
  });
  it('业务开关提交业务配置并自动获取最新状态', async () => {
    const { element } = render(ModulesView); await flush();
    element.querySelector<HTMLButtonElement>('[aria-label="启用课堂签到"]')!.click(); await flush();
    expect(api.network.updateModule).toHaveBeenCalledWith('classroom', { enabled: true, config_json: { auto_sign: true, signin_offset_minutes: -5 } });
    expect(api.network.get).toHaveBeenCalledTimes(2);
  });
  it('每十秒显示最新的课堂签到状态', async () => {
    const { element } = render(ModulesView); await flush();
    expect(element.textContent).toContain('今日暂无课程');
    vi.mocked(api.classroom.courses).mockResolvedValue({ courses: [{ course_sched_id: 'one', course_name: '高等数学', class_begin_time: '10:00', class_end_time: '11:40', signed: true }] });
    await vi.advanceTimersByTimeAsync(10000); await flush();
    expect(element.textContent).toContain('高等数学'); expect(element.textContent).toContain('已签到');
    expect([...element.querySelectorAll('button')].some(button => button.textContent === '签到')).toBe(false);
  });
  it('切换回来立即更新，页面卸载后停止获取数据', async () => {
    const { dispose } = render(ModulesView); await flush();
    window.dispatchEvent(new Event('focus')); await flush();
    expect(api.network.get).toHaveBeenCalledTimes(2);
    dispose(); cleanups.length = 0;
    await vi.advanceTimersByTimeAsync(20000);
    window.dispatchEvent(new Event('focus')); await flush();
    expect(api.network.get).toHaveBeenCalledTimes(2);
  });
  it('接口失败显示错误并在下次获取数据后恢复', async () => {
    vi.mocked(api.network.get).mockRejectedValueOnce(new Error('网络连接失败'));
    const { element } = render(ModulesView); await flush();
    expect(element.querySelector('[role="alert"]')!.textContent).toContain('网络连接失败');
    await vi.advanceTimersByTimeAsync(10000); await flush();
    expect(element.querySelector('[role="alert"]')).toBeNull();
  });
});

describe('设置', () => {
  it('后台状态更新保留正在编辑的账号与通知内容', async () => {
    const { element } = render(AccountNetworkView); await flush();
    await input(element, '#student', 'edited-student');
    await input(element, '#campus-password', 'draft-password');
    await input(element, '#bark-key', 'draft-key');
    vi.mocked(api.account.get).mockResolvedValue({ ...account, auth_status: 'authenticated' });
    await vi.advanceTimersByTimeAsync(10000); await flush();
    expect(element.querySelector<HTMLInputElement>('#student')!.value).toBe('edited-student');
    expect(element.querySelector<HTMLInputElement>('#campus-password')!.value).toBe('draft-password');
    expect(element.querySelector<HTMLInputElement>('#bark-key')!.value).toBe('draft-key');
    expect(element.textContent).toContain('已连接');
  });
  it('保存账号后连接校园认证，成功后清空密码输入', async () => {
    vi.mocked(api.account.login).mockResolvedValue({ ok: true });
    const { element } = render(AccountNetworkView); await flush();
    await input(element, '#campus-password', 'draft-password');
    element.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await flush();
    expect(api.account.update).toHaveBeenCalledWith({ student_id: '23000001', campus: '沙河', enabled: true, password: 'draft-password' });
    expect(api.account.login).toHaveBeenCalledTimes(1);
    expect(element.querySelector<HTMLInputElement>('#campus-password')!.value).toBe('');
    expect(element.textContent).toContain('校园账号已连接');
  });
});


it('执行记录显示任务结果与通知状态', async () => {
  vi.mocked(api.tasks.schedules).mockResolvedValue([]);
  vi.mocked(api.tasks.executions).mockResolvedValue([{ id: 1, module: 'classroom', task_type: 'classroom.auto_signin', status: 'succeeded', scheduled_for: '2026-11-16T02:00:00Z', result_summary: '高等数学签到成功' }]);
  vi.mocked(api.notifications.events).mockResolvedValue([]);
  const { element } = render(TasksView); await flush();
  expect(element.textContent).toContain('高等数学签到成功');
  expect(element.textContent).toContain('成功');
});
