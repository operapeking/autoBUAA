# autoBUAA

北航校园事务自动化服务，支持课堂签到、博雅课程预约报名、提醒与自主签到签退、阳光打卡进度提醒、体能锻炼状态查询，以及 Bark 推送。

## 功能

| 业务 | 提醒 | 自动操作 |
| --- | --- | --- |
| 课堂签到 | 默认提前 15 分钟提醒 | 默认课前 5 分钟开始，在课前 5 分钟至上课后 30 分钟的窗口内尝试签到 |
| 博雅课程 | 默认提前 5 分钟提醒选课，提前 15 分钟提醒签到和签退 | 预约课程在选课开始后自动报名，满额时每 30 秒查询，成功后发送 Bark 通知；已选自主签到课程在规定窗口内签到、签退 |
| 阳光打卡 | 默认每天 18:00 查询进度，学期和本周均低于平台目标时发送提醒 | 查询学期和本周完成次数 |
| 体能锻炼 | — | 每小时查询健康云当前学期 TD 合计、考勤机、活动转换及奔跑在北航次数，支持手动刷新 |

业务模块默认停用。启用后自动建立计划，课堂和博雅支持独立关闭自动签到并保留提醒。阳光打卡提醒时间、课程提醒提前分钟数均可配置。

体能锻炼学期目标默认 32 次，可在模块设置中调整，达到目标后显示“已达标”。

课堂课表每小时更新，博雅课程每 10 分钟更新。发现课程后保存提醒时间与签到窗口，签到窗口内每 30 秒尝试，成功后结束该窗口。课程取消、时间变更、完成签到或停用模块后，系统撤销相应待执行安排。

博雅课程支持预约和取消预约，选课截止后停止报名。停用博雅模块期间暂停预约，重新启用后恢复。校区过滤默认开启，根据账号校区筛选课程与选课提醒；已选课程保留显示。关闭过滤后显示全部校区课程并接收相应提醒。报名成功通知使用“自动任务执行成功时通知”规则，需要启用并配置 Bark 通道。

任务和通知保存在 SQLite 中，服务重启后恢复。提醒按事件防重；提交超时后查询平台状态确认结果。前端每 10 秒获取最新状态，页面重新获得焦点时更新。

## 部署

```bash
docker compose up -d
```

Compose 默认拉取 `ghcr.io/operapeking/autobuaa:latest` 镜像（推送 `main` 分支或 `v*` tag 时由 GitHub Actions 自动构建，同时生成 `sha-<commit>` 标签）。私有镜像需先 `docker login ghcr.io`；本地构建改用 `docker compose up -d --build`。

访问 `http://<服务器IP>:8080`，设置管理员密码。在“设置”中保存并连接校园账号，配置 Bark 通知，再在“校园事务”中启用业务。

校内使用校园网连接；校外默认使用 WebVPN，也支持 `vpn.buaa.edu.cn`。业务时间采用北京时间。

## 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `AUTOBUAA_DATA_DIR` | `./data`，容器内 `/data` | 数据持久化目录 |
| `AUTOBUAA_PORT` | `8080` | Docker Compose 对外端口 |
| `AUTOBUAA_LOG_LEVEL` | `DEBUG` | 应用日志等级 |
| `AUTOBUAA_COOKIE_SECURE` | `false` | HTTPS 部署可设为 `true` |
| `AUTOBUAA_SECRET_KEY_FILE` | `<DATA_DIR>/secret.key` | 加密密钥文件 |
| `AUTOBUAA_SCHEDULER_POLL_SECONDS` | `1.0` | 任务扫描间隔，秒 |
| `AUTOBUAA_SCHEDULER_CONCURRENCY` | `4` | 任务最大并发数 |

日志写入 `data/logs/autobuaa.log`，每个文件最大 10 MiB，保留 5 个历史文件。密码、令牌与 Bark 密钥经过脱敏。网络库日志采用 WARNING 等级。

## 数据与备份

`data/autobuaa.db` 保存账号、业务设置、计划、执行记录及通知记录；`data/secret.key` 加密校园密码、会话与 Bark 密钥；`data/captcha/` 保存登录验证码临时图片。

备份前停止服务，保存整个 `data` 目录。恢复时同时恢复数据库与密钥。

```bash
docker compose stop
tar -czf autobuaa-backup.tar.gz ./data
docker compose start
```

升级时重新构建并启动，应用启动时执行数据库迁移。

```bash
docker compose up -d --build
```

## 本地开发

```bash
uv sync --extra dev
uv run --extra dev pytest backend/tests -q
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend test
```

```bash
uv run uvicorn --app-dir backend --factory app.main:create_app --host 127.0.0.1 --port 8080
npm --prefix frontend run dev
```
