# Binance 跟单实时监控（Render 免费版）

10 秒级轮询币安跟单接口（默认 5 秒，检测到成交后 3 秒爆发 2 分钟），有新成交时推送到 Telegram。

## 为什么是 Render

- Render 免费 Web Service：512MB / 0.1 CPU，24 小时常驻进程（需外部保活）
- 免费额度：每个 workspace 每月 750 实例小时（31 天 = 744 小时，刚好够）
- 免费实例 15 分钟没有入站请求就会休眠，所以需要每分钟 ping 一次 `/health`

保活由同账号的 Cloudflare Worker 定时任务完成（`* * * * *`），不额外注册平台。

## 部署

1. Render 用 GitHub 登录 → New → Blueprint → 选本仓库
2. 填写两个环境变量：
   - `TG_BOT_TOKEN`
   - `TG_CHAT_ID`
3. 部署完成后访问 `https://<服务名>.onrender.com/health` 应返回 `{"ok":true,...}`

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TG_BOT_TOKEN` | 必填 | Telegram Bot Token |
| `TG_CHAT_ID` | 必填 | 接收推送的 chat id |
| `PORTFOLIO_ID` | `5075281354358777856` | 被跟单的组合 ID |
| `CHECK_INTERVAL` | `5` | 常规轮询间隔（秒，带 ±15% 抖动） |
| `BURST_INTERVAL` | `3` | 检测到新成交后的爆发间隔（秒） |
| `BURST_WINDOW` | `120` | 爆发模式持续时间（秒） |
| `JITTER` | `0.15` | 间隔随机抖动比例 |
| `MAX_RUNTIME` | `0` | 0 = 永久运行 |
| `SEND_STARTUP` | `1` | 启动时是否发一条启动消息 |
| `PORT` | Render 注入 | 健康检查端口 |

## 注意

- 免费实例随时可能被重启：进程重启后会重新同步最近 5 笔作为基线，不会重复推送。
- 免费实例的文件系统是临时的，本程序不依赖本地持久化。
- Render 可能对「异常高的出站流量」限流，本项目每分钟约 6 次请求，远低于阈值。
