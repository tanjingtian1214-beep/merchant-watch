# 洛克王国：世界 · 远行商人监控（云端版）

7×24 独立运行，不依赖你的电脑是否开机。命中「炫彩蛋 / 棱镜球 / 祝福项链」时发邮件通知。

---

## 核心机制（已在实测中验证）

- 远行商人每日 **08:00–24:00** 出摊，**00:00–08:00** 关闭
- 每日 **4 轮**刷新：08-12 / 12-16 / 16-20 / 20-24
- 数据源：好游快爆官方「远行商人查询器」
  `https://www.onebiji.com/hykb_tools/comm/lkwgmerchant/preview.php?id=1&immgj=0`
- 源站**只渲染当前轮**，商品 li 的 `data-time` = 该轮**结束时刻**，据此反推时段标签
- 源站会**同时渲染上一轮与当前轮**，必须按 `data-time` 分组后再挑选，否则会误报
- 目标商品匹配采用双模式：
  - **精确**：棱镜球必须完全等于「棱镜球」——避免误命中「宝匣棱镜球」（S5 通行证道具）、「织梦棱镜球」（S1 赛季限定），二者均非远行商人商品
  - **包含**：炫彩蛋/炫彩精灵蛋、祝福项链/祝福项坠（后者是游戏内官方名）

## 通知规格

| 项 | 值 |
|---|---|
| 收件人 | 19812138373@163.com、1123404495@qq.com |
| 标题 | 远行商人来货了 |
| 正文 | 当前时段全部在售商品（商品名 + 限购 + 热销标注，**不含洛克贝价格**） |
| 漏轮提醒标题 | 远行商人监控·漏轮提醒 |
| 触发 | 仅命中目标商品时发送；无命中静默 |
| 去重 | 同一「日期+时段+商品组合」只发一次 |

---

## 部署方式 A：GitHub Actions（推荐，零成本零运维）

不需要任何服务器，GitHub 托管执行。公开仓库免费无限，私有仓库每月 2000 分钟免费额度（本项目每日 4 次 × 约 1 分钟 ≈ 120 分钟/月，远低于额度）。

### 步骤

1. 新建一个 GitHub 仓库（**Public** 可避免 Actions 额度限制，也更省事）

2. 上传以下文件（保持目录结构）：
   ```
   merchant_watch_cloud.py
   .github/workflows/merchant-watch.yml
   ```

3. 添加 Secrets：
   **Settings → Secrets and variables → Actions → New repository secret**
   - `SMTP_USER` = `19812138373@163.com`
   - `SMTP_AUTH_CODE` = 你的 163 客户端授权码

4. 手动触发一次验证：**Actions → 洛克王国远行商人监控 → Run workflow**

5. 确认无误后即可等待自动运行

### 调度时间

工作流使用 **UTC** cron，已换算为北京时间：

| 北京时间 | UTC cron | 对应轮次 |
|---|---|---|
| 08:03 | `3 0 * * *` | 08-12 |
| 12:03 | `3 4 * * *` | 12-16 |
| 16:03 | `3 8 * * *` | 16-20 |
| 20:03 | `3 12 * * *` | 20-24 |

合并为一行：`cron: '3 0,4,8,12 * * *'`

### 状态持久化

去重与漏轮检测依赖 `state/merchant_state.json`，通过 `actions/cache` 在运行间持久化。

---

## 部署方式 B：VPS / 树莓派 / NAS（systemd）

适合已有 Linux 主机的情况，且能真正做到**关机后补跑**（systemd `Persistent=true`）。

### 步骤

```bash
# 1. 部署代码
sudo mkdir -p /opt/merchant-watch
sudo cp merchant_watch_cloud.py /opt/merchant-watch/
sudo cp deploy/merchant-watch.service deploy/merchant-watch.timer /etc/systemd/system/

# 2. 写入凭据（权限必须 600）
sudo mkdir -p /etc/merchant-watch
sudo sh -c 'printf "SMTP_USER=19812138373@163.com\nSMTP_AUTH_CODE=你的授权码\n" > /etc/merchant-watch/env'
sudo chmod 600 /etc/merchant-watch/env

# 3. 创建专用用户
sudo useradd -r -s /usr/sbin/nologin merchant-watch
sudo chown merchant-watch:merchant-watch /opt/merchant-watch

# 4. 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable --now merchant-watch.timer

# 5. 验证
systemctl list-timers merchant-watch.timer
sudo systemctl start merchant-watch.service && journalctl -u merchant-watch -n 20
```

---

## 本地试跑

```bash
export SMTP_USER=19812138373@163.com
export SMTP_AUTH_CODE=你的授权码
python merchant_watch_cloud.py
```

输出为 JSON：

```json
{"ok": true, "time_bj": "...", "slot": "12-16", "items": ["..."], "hits": [], "sent": false}
```

---

## 漏轮检测

源站只保留当前轮，**错过的时段商品无法事后补查**。因此脚本会记录每日已检查的时段，若发现某轮从未被执行，会发一封漏报提醒，让你知道漏了、而不是静默错过。

约束（避免误报）：
- 只统计部署之后结束的时段
- 只统计当前自然日，部署当日不追溯昨日

---

## 常见问题

**Q：163 发信提示认证失败？**
A：授权码失效或未开启 SMTP 服务。登录 https://email.163.com → 设置 → POP3/SMTP/IMAP → 开启 POP3/SMTP 服务并重新生成授权码。授权码不是登录密码。

**Q：QQ 邮箱收不到 / 在垃圾箱？**
A：QQ 对陌生发件人过滤较严。把发件人 `19812138373@163.com` 加入 QQ 邮箱白名单：设置 → 垃圾箱 → 垃圾箱过滤设置 → 白名单。

**Q：GitHub Actions 没按时跑？**
A：公开仓库的定时任务仅从默认分支触发，且 GitHub 对 cron 有排队延迟（通常几分钟内）。检查 Actions 页面的运行记录。

**Q：能同时发飞书吗？**
A：云端无飞书 CLI 授权，只能走邮件。本机版（`../merchant_watch.py`）可同时发飞书 + 邮件，两者互为补充。

---

## 与本机版的关系

| | 本机版 `../merchant_watch.py` | 云端版 `merchant_watch_cloud.py` |
|---|---|---|
| 运行位置 | 你的电脑（WorkBuddy 定时任务） | 云端 7×24 |
| 电脑关机 | ❌ 不运行 | ✅ 不受影响 |
| 飞书通知 | ✅ | ❌（无授权） |
| 邮件通知 | ✅ | ✅ |
| 漏轮检测 | ❌ | ✅ |

建议**两者同时启用**：云端负责不漏，邮件通道两者都能发；本机版额外提供飞书即时提醒。
