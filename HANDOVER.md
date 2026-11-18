# 交接说明：把助手迁到 Mac

这份文件是给"未来在 Mac 上接手的人/agent"看的上下文。当前实现已在 Linux 上跑通真机邮件、分类提醒、手机 Web、每日复盘。

## 1. 现状

| 能力 | 状态 | 关键文件 |
| --- | --- | --- |
| Markdown 资料索引（jieba + FTS5，`path:line`） | ✅ | `knowledge.py` |
| 只读 IMAP 同步（去重/线程/失败重试/大附件截断） | ✅ | `imap.py`, `mail.py` |
| 7 类归档 + 分级提醒 | ✅ | `rules/mail_rules.yaml`, `reminders.py` |
| 手机 Web（登录、收件箱、改判学规则、检索、提醒确认） | ✅ | `web.py`, `templates/` |
| 手机确认稿（编辑 + revision 校验 → 归档） | ✅ | `flows.py`, `templates/draft.html` |
| 每日复盘（pi/codex 会话、纠错案例、LLM 归纳） | ✅ | `agentlog.py`, `review.py` |
| 任务列表 / 回复邮件草稿 / 发送 | ⏳ 未做 | 端口在 `ports.py` 预留 |

- 仓库根就是本机项目目录（Mac 上已把原来的 `lab1/` 内容上移到根）；CI 在 `.github/workflows/ci.yml`，直接在仓库根跑
- 最近提交：`37d696a` 补充 Mac 迁移交接说明；`f85e381` 默认只监听本机；`5dd1c7e` key 链接登录；`e800167` 手机确认；`6420174` 分类提醒
- 61 个测试；`ruff check` / `ruff format --check` 与 CI 一致

## 2. 迁移什么、不迁移什么

**必须带过去（都在 Git 里）**

- 整个仓库（本机项目目录，由 `your-assistant/lab1` 上移而来）
- `rules/lessons.md`、`rules/mail_rules.yaml`（规则要按 Mac 上的实际情况再改）

**需要手动带（被 gitignore，不能提交）**

| 路径 | 说明 | 建议 |
| --- | --- | --- |
| `.env` | 邮箱客户端专用密码、Web key、session secret | **在 Mac 上重新生成 key/secret**，邮箱密码照抄 |
| `vault/notes/` | 你的资料 | 带上 |
| `memory/cases/` | 纠错案例 | 带上（可进 Git） |
| `data/` | 41MB：SQLite + 原始邮件缓存 | **建议不搬**，到 Mac 上重建（见下） |

**为什么建议重建 `data/`**

数据库里存的是**绝对路径**（`/home/hia/proj/.../vault/...` 和 `.../data/mail/...`）。换机器后这些引用会失效。
重建成本很低：

```bash
uv run your-assistant index --rebuild      # 重新索引 vault
uv run your-assistant sync                 # 重新拉最近 50 封（只读）
```

如果一定要搬，务必先停服务，并复制 `assistant.db`、`assistant.db-wal`、`assistant.db-shm` 三个文件（或用 `sqlite3 assistant.db ".backup out.db"`），否则可能拿到不一致的快照。

## 3. Mac 上从零跑起来

```bash
# 1) 安装 uv（任选其一）
brew install uv
# 或 curl -LsSf https://astral.sh/uv/install.sh | sh

# 2) 依赖（.python-version 固定 3.12，uv 会自动装）
cd /path/to/your-assistant
uv sync --locked --group dev

# 3) 配置
cp .env.example .env
chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 填 ASSISTANT_SESSION_SECRET
python3 -c "import secrets; print(secrets.token_urlsafe(12))"   # 填 ASSISTANT_ACCESS_KEY（或留空=ASSISTANT_PASSWORD）
# 填 ASSISTANT_EMAIL_ADDRESS / ASSISTANT_EMAIL_PASSWORD / ASSISTANT_PASSWORD

# 4) 体检（会真的连 IMAP 和 LLM）
uv run your-assistant doctor --net

# 5) 首次数据
uv run your-assistant index
uv run your-assistant sync

# 6) 本机启动（默认只监听 127.0.0.1）
uv run your-assistant serve
```

在本机打开 `uv run your-assistant link` 打印的 `http://127.0.0.1:8000/login?key=...` 即可。

## 4. Mac 特有的坑

1. **没有 `setsid`**（那是 Linux 的）。后台运行用：
   ```bash
   nohup uv run your-assistant serve > data/serve.log 2>&1 &
   ```
   或者用 `launchd`（见第 9 节）。
2. **默认只监听本机**。手机要访问必须显式授权：
   ```bash
   uv run your-assistant serve --allow-lan        # 单次
   # 或 .env: ASSISTANT_ALLOW_LAN=true       # 长期
   uv run your-assistant link --allow-lan
   ```
   未授权时对 `0.0.0.0`/局域网 IP 会直接拒绝启动。
3. **macOS 防火墙 / 本地网络权限**：第一次监听局域网时系统可能弹窗，要允许；
   "系统设置 → 隐私与安全性 → 本地网络"里确认终端/Python 有权限。
   另外校园网可能有 AP 客户端隔离，手机和 Mac 未必互通；必要时用手机热点。
4. **IPv6/IPv4**：代码会遍历所有解析地址逐个尝试（在 Linux 上 IPv6 超时、IPv4 可用）。
   Mac 上通常直接可用；如果邮件同步偶发超时，看 `data/serve.log` 和 `your-assistant sync` 输出。
5. **LLM key**：默认从 `~/.pi/agent/models.json` 的 `deepseek` provider 读取
   （`ASSISTANT_PI_PROVIDER` 可换）；也可用 `ASSISTANT_LLM_BASE_URL/API_KEY/MODEL` 覆盖。
   Mac 上 shell 若设置了 `ALL_PROXY`/`HTTPS_PROXY`，默认**不会继承**（`trust_env=False`）；
   需要代理就设 `ASSISTANT_LLM_PROXY=http://127.0.0.1:7890`。
   `2404` 上的会话已随仓库迁到本机：默认读本机 `~/.pi/agent/sessions` 与 `~/.codex/sessions`，
   `.env` 的 `ASSISTANT_REMOTE_AGENT_HOSTS` 留空；只有要读**另一台**机器时才填 ssh 别名。
7. **时区**：默认 `Asia/Shanghai`；每日复盘 `ASSISTANT_DAILY_REVIEW_AT=20:00` 按本地时区。
8. **端口占用**：只跑一个 `your-assistant serve` 实例（内置 APScheduler 会同步邮件/提醒/复盘）。
   重复启动会 `Address already in use`。

## 5. 安全约定（必须保持）

- 邮箱**只读**：仅 `SELECT INBOX (readonly)` / `UID SEARCH` / `BODY.PEEK`；
  不存在 `STORE/COPY/MOVE/EXPUNGE/APPEND`，也没有 SMTP/POP3 实现。
- `ASSISTANT_ALLOW_SEND` / `ASSISTANT_ALLOW_DELETE` 默认 false，且即使打开也没有适配器。
- **不要在未经用户允许时监听局域网**；默认 `127.0.0.1`。
- `.env` 权限 600；不进 Git、不进日志。
- Web 登录：`/login?key=<ASSISTANT_ACCESS_KEY>` 或密码表单；成功后写 30 天
  HttpOnly + SameSite=Lax 的签名 cookie；每个写操作校验 CSRF。
- 换 key 只影响新登录；要让已登录设备全部失效，需同时更换 `ASSISTANT_SESSION_SECRET`。

## 6. 运行机制（方便排查）

- `your-assistant serve` 启动 FastAPI + APScheduler，后台任务：
  - 每 `ASSISTANT_MAIL_INTERVAL_SECONDS`（默认 300s，即 5 分钟）同步邮件
  - 每 10 分钟增量索引
  - 每 60 秒检查到期提醒
  - 每天 `ASSISTANT_DAILY_REVIEW_AT` 生成复盘
- 邮件分类：`rules/mail_rules.yaml` 的 `contacts.advisors` → `learned` → `base`（顺序敏感）。
  论文/导师 = 立即提醒（0/15m/45m/2h/6h，直到确认）；活动 = 提醒一次；其余不提醒。
- 手机确认：`drafts` 表保存正文与 `sha256(正文)[:12]` 版本号。
  确认时要求「页面提交的 revision == DB 当前 revision == 当前正文哈希」，
  不一致返回 409；成功后写入 `vault/outbox/`、关闭该邮件提醒。
- "未读" = 服务器 UNSEEN ∪ 回填之后新到且本地未确认（否则其他客户端已读后界面会永远空白）。

## 7. 代码地图

```
src/your_assistant/
  config.py     环境变量 → Settings（含 loopback/allow_lan、LLM、capabilities）
  models.py     领域对象与 Category/Notify/Importance
  ports.py      唯一接缝：MailSource/MailSender/Retriever/Llm/Notifier/AgentLogSource...
  db.py         SQLite 连接 + 6 个迁移（事件/索引/邮件/提醒/草稿）
  knowledge.py  Markdown 分块、jieba、FTS5、行号引用
  imap.py       只读 IMAP 适配器（PEEK、UIDVALIDITY、pending 重试、大附件截断）
  mail.py       MailRepository + RuleClassifier + 改判学习 + reclassify
  reminders.py  提醒计划/到期/确认（POLICY 常量在文件顶部）
  flows.py      确认稿生成、保存、revision 校验、归档
  agentlog.py   pi/codex JSONL 解析（含可选的 ssh 远端拉取）
  review.py     每日复盘（LLM 可选，失败降级）
  web.py        FastAPI 路由 + session/CSRF
  worker.py     APScheduler 组装
  cli.py        doctor/index/sync/reclassify/remind/digest/serve/link
```

## 8. 已知限制 / 下一步

- 没有任务列表、没有"回复邮件草稿"、没有发送（需求要求只读）。
- 提醒只在站内；可加 `Notifier` 适配器（ntfy/Telegram）。
- 分类规则是从你最近的邮件推断的：`contacts.advisors` 还是空的，
  在 Mac 上把导师/合作者地址填进去，论文类提醒才会真正命中。
- 手机端不能新建/编辑笔记（资料放 `vault/notes/`）。
- 没有向量检索；`Retriever` 端口预留。

## 9. launchd 常驻示例（可选）

`~/Library/LaunchAgents/com.hia.your-assistant.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.hia.your-assistant</string>
  <key>WorkingDirectory</key><string>/Users/YOU/fffile/personal_agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/uv</string>
    <string>run</string><string>your-assistant</string><string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/fffile/personal_agent/data/serve.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/fffile/personal_agent/data/serve.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.hia.your-assistant.plist
```

注意：`uv` 路径用 `which uv` 的实际结果；plist 里不要写 `--allow-lan`，除非你确认要手机访问。

## 10. 排障

```bash
uv run your-assistant doctor --net          # 配置/IMAP/LLM
uv run your-assistant sync                  # 看同步摘要与错误
uv run your-assistant remind                # 看提醒队列
tail -50 data/serve.log          # Web/调度日志
ss -tlnp | grep 8000             # 是否在监听（Mac 用 lsof -i :8000）
sqlite3 data/assistant.db 'select category, notify, count(*) from mail_messages group by 1,2;'
```