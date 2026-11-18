# 会成长的个人助手 — MVP 分析与开发计划

> 状态：草案（待确认）。对应需求：`lab1/README.md`（最新，11-17）与 `../README.md`（11-12）。

## 1. 可行性结论

**可行，而且用一台常开的 Linux 机器 + 手机浏览器就能跑通全部核心演示。** 关键技术点全部已验证：

| 验证项 | 结果 |
| --- | --- |
| Python 3.12 + venv 依赖（fastapi/apscheduler/jieba/httpx/imaplib…） | 已就绪，`src` 可编辑安装（需重建 `pyproject.toml` / `uv.lock`） |
| SQLite 3.45 + FTS5（中文检索方案） | 支持 `fts5` / `unicode61` |
| jieba 中文分词 | 可用（首次加载 0.4s） |
| 上科大 IMAP `imap.shanghaitech.edu.cn:993` | **TCP 可达，返回 `* OK Coremail System IMap Server Ready`**（IPv4 `10.15.44.251`） |
| 本机局域网地址（手机访问） | `10.19.127.218/22`，手机连校园网即可访问 Web UI |
| 历史 agent prompt 数据源 | `~/.pi/agent/sessions/**/*.jsonl` 共 10 个会话，schema 稳定（`message` + role/content[]） |
| LLM 可用性 | **无 Ollama、无本地模型** → 所有 LLM 功能必须可降级（关键设计约束） |
| 推送/公网通道 | 无 Tailscale → 走局域网 Web；推送做成可插拔（ntfy/Telegram 后续接入） |
| 邮件凭据 | 无 `.env` → 真实收发无法在当前环境验证，需要你提供客户端专用密码 |

结论：**MVP 用"确定性规则 + 可插拔 LLM"实现，不依赖任何模型也能完整演示。**

## 2. 需求冲突与取舍（最重要的判断）

1. **邮件能不能"发"？两版 README 冲突。**
   - 旧版（`../README.md`）：要"拟好回复草稿，可编辑后直接发送"。
   - 新版（`lab1/README.md`）：**"不允许发送和删除邮件，只负责呈现未读、归档区分重要性"**。
   - 按"最新优先"取**只读**。但把能力做成 `Capabilities` 配置 + 端口：
     `MailSource`（读）与 `MailSender`（写）分离，UI 按 capability 渲染按钮。
     以后需求再变回"要发送"，只需实现 `MailSender` 适配器并打开开关，**不改 web/domain 层**。这正是本次架构要抵御的变化。
   - 同理"删除/移动文件夹"不实现，且 IMAP 一律用 `BODY.PEEK[]`，不写 `\Seen`；"已读"状态只存在本地库里。
2. **"手机端联动"不能依赖桌面端常开窗口。** → 主体是响应式 Web（可加到主屏，PWA manifest），服务常驻、APScheduler 跑后台任务。
   桌面聊天框作为"可选输入通道"：直接解析 pi 会话 JSONL（需求 5 本来就要用），无需 AppleScript；输出方向走 vault + Web，后续再补 pi extension。
3. **"能力组合"的完整流程必须具体。** 选定一条（见 §6），其余组合留作 skill 脚本扩展。
4. **"用 Git 管理记忆"与 `.gitignore` 冲突。** `../.gitignore` 忽略了 `lab1/vault/`。
   → 拆两处：`vault/`（私人数据、生成物，忽略）与 `memory/ + rules/`（沉淀的规则、案例、技能，**入库可 diff**）。

## 3. MVP 范围

### 做（MVP-1，约 3.5 天）
| # | 能力 | 为什么必须 |
| --- | --- | --- |
| A | Markdown 知识库：增量索引、中文检索、`path:line` 原文定位 | 需求 1 的核心，也是其他流程的上下文来源 |
| B | 只读 IMAP 持续同步：去重、线程聚合、规则分类重要性、未读视图 + 详情页 | 需求 2，演示价值最高 |
| C | 手机可用 Web：登录、任务、进度、草稿编辑、确认（版本哈希）、提醒 | 需求 3，是"交互界面"的落地 |
| D-1 | 一条端到端流程：邮件 → 任务 → 知识检索 → 简报（带引用） → 手机确认 → 归档 | 需求 4 的"至少一条" |
| E-1 | 规则沉淀：`rules/lessons.md` 注入 + 邮件重标即追加规则 + 每日 agent 复盘草稿 | 需求 5 的最小闭环 |

### 做（MVP-2，约 2 天，时间够再上）
- D-2：重要邮件的多次提醒（0/30m/2h，可 snooze/done，直到确认完成）+ 可选 ntfy 推送。
- E-2：复盘页可"一键提议成规则"，确认后写入 `rules/lessons.md` 并 git 提交。
- B-2：HTML 邮件清洗、附件名索引、全文搜索。
- 可选 OpenAI 兼容 LLM（deepseek/Ollama）：只做分类润色与复盘摘要，默认关闭。

### 明确不做（并说明理由）
| 不做 | 理由 / 后续如何加 |
| --- | --- |
| 发送邮件、删除邮件、服务器端归档 | 新版需求明确禁止；`MailSender` 端口 + capability 开关预留 |
| 向量/语义检索 | FTS5 + jieba 已够用；`sqlite-vec` 已在环境里，`Retriever` 端口直接加实现 |
| AppleScript / Memos 桥、桌面推送 | 无法在当前环境验证；`Channel` 端口预留，输入侧已用 pi 会话日志替代 |
| 真正的 PWA 离线 / Web Push | 成本高于收益；`Notifier` 端口 + ntfy 更省事 |
| IMAP IDLE 实时 | 5 分钟轮询足够（可配）；`MailSource.sync()` 契约不变 |
| 多用户、权限体系、加密 | 单用户场景；单密码 + 签名 cookie 已够 |
| PDF/图片 OCR、日历双向同步 | 边缘需求，先记录在 README "已知限制" |

## 4. 架构：同步内核 + 端口适配（六边形，但不过度分层）

**一条硬性规则：领域/服务层全部同步函数，异步只发生在 HTTP 边界。**
理由：sqlite3、imaplib、文件 IO 都是同步的；`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 下没有 pytest-asyncio，异步测试会变成负担。FastAPI 用 `def` 端点自动跑线程池，APScheduler 用线程池，足够了。

```
web(HTML/JSON)  cli  scheduler         ← 适配层（可替换/可新增）
      │            │        │
      └──── flows ──┴── services ──────  ← 用例层：mail_brief / reminders / review
                     │
        ports.py（Protocol）: MailSource, MailSender, Notifier, Llm,
                              Retriever, KnowledgeStore, AgentLogSource, Clock
                     │
      adapters: imap.py / fake.py / rules.py / fts.py / ollama.py / ntfy.py
                     │
      db.py(SQLite) + vault/ + rules/ + memory/   ← 持久化（全部人可读）
```

### 目录（12–16 个文件，刻意保持小）
```
lab1/
  pyproject.toml  uv.lock  .env.example  README.md  PLAN.md
  src/your_assistant/
    config.py        Settings（env）+ Capabilities（read/draft/send/delete/notify）
    models.py        领域 dataclass：MailMessage/Thread/Task/Hit/Lesson/Draft/Event
    ports.py         所有 Protocol（≤120 行，是全项目的"接缝"）
    db.py            connect/migrate/schema（单文件迁移即够）
    knowledge.py     索引 + 分块 + 检索 + 引用（含 jieba/HTML/Markdown 处理）
    mail.py          仓库层（去重/线程/本地已读）+ 规则分类 + 反馈学习
    imap.py          IMAP 只读适配器（PEEK、UIDVALIDITY 处理）
    tasks.py         任务/提醒/确认（revision hash）
    memory.py        lessons 读取注入、案例写入、pi 会话解析、每日复盘
    flows.py         跨工具编排：mail → brief → confirm → archive
    model.py         Llm 端口实现：NullLlm（默认）/ OllamaLlm
    worker.py        APScheduler 组装（index/mail/digest/remind 四个 job）
    web.py           FastAPI 应用 + 路由 + 模板（Jinja2）
    cli.py           serve / index / sync / digest / doctor / demo
  rules/lessons.md  rules/mail_rules.yaml      ← 入库、可手改
  memory/cases/*.md  memory/skills/*.md        ← 入库、可手改
  tests/{conftest,test_knowledge,test_mail,test_web,test_review}.py
  vault/  data/                                 ← 忽略（私人数据）
```

### 关键端口（对抗需求变化的接缝）
```python
class MailSource(Protocol):  # 只读契约，根本没有 delete/move 方法
    def sync(self, since: datetime | None) -> SyncResult: ...


class MailSender(Protocol):  # MVP 不提供实现；capability=send 时才装配
    def send(self, draft: Draft) -> DeliveryResult: ...


class Retriever(Protocol):  # 关键字 → 向量/混合，只换实现
    def search(self, q: str, k: int = 8) -> list[Hit]: ...


class Notifier(Protocol):  # 站内 → ntfy/Telegram/邮件
    def notify(self, event: Event) -> None: ...


class Llm(Protocol):  # NullLlm 永远返回 None → 全部功能可降级
    def complete(self, task: str, prompt: str, schema: dict) -> dict | None: ...


class AgentLogSource(Protocol):  # pi JSONL → 以后换别的 agent 日志
    def sessions_between(self, start, end) -> Iterable[Session]: ...
```

## 5. 数据与记忆布局

- `data/assistant.db`（SQLite，可删可重建）
  - `documents(id, path, mtime, size, sha)`；`blocks(doc_id, start_line, end_line, text, tokens)`
  - `blocks_fts`（FTS5：`tokens` 用 jieba 预分词，`text` 保留原文用于自绘 snippet）
  - `mail_messages(account, uidvalidity, uid, message_id, thread_key, from, subject, date, body_text, importance, tags, seen_local, raw_path, UNIQUE(message_id))`
  - `threads(thread_key, subject_key, last_at, participants, summary)`
  - `tasks(id, title, due_at, status, source_kind, source_ref, checklist_json)`；`reminders(task_id, fire_at, fired_at, channel, ack)`
  - `drafts(id, kind, target_ref, revision, body, confirmed_at)`；`events(kind, payload_json, at)`（审计）
  - `feedback(target_ref, old_label, new_label, at)`（用于学规则）
- `vault/notes/`（你的资料，被索引）、`vault/briefings/`（生成）、`vault/reviews/`（每日复盘）
- `rules/lessons.md`：人写的经验（"不要把报名截止时间当成活动开始时间"），全部 LLM prompt 前置注入 + Web 展示命中项
- `rules/mail_rules.yaml`：`when(from_contains / subject_regex / has_list_id) → importance/tags`，
  由"用户重标邮件"自动追加到 `learned:` 段（带 `at` 和证据邮件 id），可读可回滚
- `memory/cases/YYYY-MM-DD-*.md`：成功流程/纠错案例（带复盘），新会话作为 skill 上下文
- **所有"记忆"都是 Markdown/YAML 文件；DB 只当索引与缓存，删库不丢规则**

## 6. 一条反复使用的完整流程（演示主线）

1. `mail-sync` 拉到一封未读"×× 讲座报名通知"，规则判定 `importance=high, tags=[deadline,event]`，抽 `due=报名截止`。
2. 自动建任务 `报名 ×× 讲座`，附邮件引用 `mail:<message_id>`。
3. 流程检索知识库（`Retriever`）→ 拿 3 条带 `path:line` 的资料；再匹配 `lessons.md` 中"截止时间≠活动时间"。
4. 生成 `vault/briefings/2026-11-18-lecture.md`：邮件要点 / 关键时间（标注来源）/ 相关资料（引用）/ 待确认项。
5. 手机打开 Web：任务详情看到步骤与进度 → 编辑"我的确认/草稿"文本框 → 点确认（提交 `revision` 哈希，过期即拒绝）。
6. 确认后：任务 `done`、事件归档、简报追加"已确认 @时间"，并把"这条时间字段该如何读"的回执写入 `memory/cases/`。
7. 若 `high` 且未确认：提醒 job 在 0/30m/2h 提醒（Web 红点 + 可选 ntfy），直到用户 ack / done。
8. 下次同类邮件：`mail_rules.yaml` 的 learned 规则直接命中，分类更准（"根据历史归档纠正变聪明"）。

## 7. 里程碑、验收与测试

| 里程碑 | 内容 | 验收（命令 / 测试） | 估时 |
| --- | --- | --- | --- |
| **M0 骨架** | pyproject(hatchling)+uv.lock+CI 对齐、config、db 迁移、CLI 空壳、初始 commit | `uv run --locked ruff check . && ruff format --check . && pytest -q` 全绿；`your-assistant doctor` 打印环境体检 | 0.5d |
| **M1 知识库** | 索引/增量/删除、jieba+FTS5、`path:line` 引用、`GET /search`、`your-assistant index` | `test_index_follows_edits_deletions_and_cites_lines`、`test_long_markdown_lines_remain_searchable`、中文查询命中 | 1d |
| **M2 邮件只读** | IMAP PEEK 同步、UIDVALIDITY 重置、去重、线程、规则分类、未读列表/详情、本地已读 | `test_sync_is_read_only_and_deduplicates_after_uidvalidity_change`、`test_empty_initial_inbox_keeps_future_messages`、`test_sync_failure_is_visible`；`your-assistant sync --fixture` 离线可跑 | 1.5d |
| **M3 手机 Web** | 登录/CSRF、任务、草稿 revision 确认、提醒列表、PWA manifest、局域网监听 | `test_login_csrf_preview_and_confirm`、`test_dismiss_restore_invalidates_previous_preview`；手机实机截图 | 1d |
| **M4 流程串联** | `flows.py`：邮件→任务→简报→确认→归档；提醒循环；`your-assistant demo` 一键造样例数据 | `test_brief_flow_cites_mail_and_notes`、端到端 demo | 1d |
| **M5 学习闭环** | pi 会话解析、每日复盘 Markdown、重标→规则、lessons 注入与命中展示 | `test_review_extracts_corrections`、`test_relabel_appends_rule_and_changes_next_classification` | 1d |
| **M6 文档** | `lab1/README.md` 追加运行/验证/已知限制章节（保留原需求原文），`docs/decisions.md` 记录取舍 | 新人照 README 5 分钟跑起来 | 0.5d |

MVP-1 = M0–M4（含薄版 M5：仅规则注入 + 复盘草稿生成），约 **4 天**，每天结束都可演示。

### 测试策略
- 全部 **同步测试**；不联网、不依赖真实邮箱与模型：`FakeMailSource`（内置 8 封样例 `.eml`）+ `Clock` 注入 + `tmp_path` vault。
- Web 用 `TestClient`；断言"未确认不发、确认只发一次、revision 过期拒绝"这类幂等语义。
- 抽取旧版需求时代的关键回归点（去重/UIDVALIDITY/并发确认/不确定投递不重试）为契约测试，**但发送相关用例在 capability=read-only 下跳过**（保留 xfail/skip 标记，将来打开发送即启用）。
- CI 与 `.github/workflows/ci.yml` 完全对齐：`uv sync --locked --group dev` 要求提交 `uv.lock`，`[dependency-groups].dev = ["pytest","ruff"]`。

## 7.5 进度（滚动更新）

| 里程碑 | 状态 | 已验证 |
| --- | --- | --- |
| M0 骨架 | ✅ 已提交 | `your-assistant doctor --net` 通过；LLM 实测 `deepseek-flash` 可用 |
| M1 知识库 | ✅ 已提交 | 增量索引、中文分词、`path:line` 引用；30 个测试 |
| M2 只读邮件 | ✅ 已提交 | 真机 50 封入库 / 44 线程 / 2 未读；只读校验；大附件只取 256KB；失败 UID 待重试 |
| M3 手机 Web | ✅ 已提交 | 登录/CSRF、收件箱、详情、改判学规则、检索引用、状态页、PWA manifest |
| M4 每日复盘 | ✅ 已提交 | pi+codex 解析、纠错案例、LLM 归纳与确定性降级、20:00 定时任务；真机生成 `vault/reviews/2026-11-17.md` |
| M4.5 分类与提醒 | ✅ 已提交 | 7 类归档；论文/导师立即+反复提醒、活动提醒一次、其余不提醒；真机 50 封分类与 1 条活动提醒验证 |
| M5 任务与组合流程 | ⏳ 进行中 | 计划：任务→简报→确认→归档、手机端确认 |
| M6 文档 | ⏳ | README 运行说明 + 已知限制 |

**真机发现（已写进实现）**
- 邮箱服务器 IPv6 超时、IPv4 可达；登录做了 3 次退避重试。
- 用户邮箱当前 0 封 UNSEEN（其他客户端已读），所以"未读"= 服务器未读 ∪ 回填后新到且本界面未确认，否则界面会永远空白。
- 26MB 级别的群发邮件只取头部 + 前 256KB 正文，避免同步卡死。
- 失败 UID 记入 `pending`，下次同步优先重试。
- 2404 就是本机，复盘直接读本机 pi/codex 目录即可；远端拉取（`ASSISTANT_REMOTE_AGENT_HOSTS`）仅在读其他机器时需要，当前留空。后续整体部署到 Mac。
- 最近 50 封邮件里没有真正的投稿系统回信；分类实测：宣讲会 13、学校公示 12、其他 14、论坛 7、活动 3、外部广告 1、论文/导师 0。图书馆"论文写作培训"已归为学校公示，避免误触发立即提醒。
- 提醒节奏：即时类 0/15m/45m/2h/6h，普通类 1 次；点"我知道了"即停止。

## 8. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 无真实邮箱凭据，同步逻辑可能撞上 Coremail 细节（UTF-7 文件夹名、GBK 头、HTML 结构） | 先把 `MailSource` 契约、去重、PEEK、编码解码做好；`your-assistant doctor --mail` 做连通性/登录/文件夹探测；真实邮箱到位后只调 `imap.py` 一个文件 |
| 服务器只读要求被误伤（标已读/移动） | `MailSource` 接口不暴露写操作；代码审查点：只允许 `FETCH ... BODY.PEEK`、`SEARCH`、`LIST`、`SELECT(readonly=True)` |
| LLM 缺席导致"智能"不足 | 规则文件 + 反馈学习承担分类；简报/复盘用模板生成；LLM 仅做可选润色（`NullLlm` 默认） |
| FTS5 中文效果差 | jieba 预分词做索引列、原文字段自绘高亮；查询无结果时回退 `LIKE`；测试覆盖长行/中英混排 |
| 需求再变（要发送 / 要向量 / 要手机 App） | 变化点全部收敛在 `ports.py` + `config.Capabilities` + `web.py` 路由，用例层不动；本计划 §2.1 已给出"要发送"的改造路径 |
| 隐私：把 agent 会话（可能含密钥）索引进库 | 复盘源**只读** `sessions/**/*.jsonl`；黑名单 `models.json/auth.json/.env`；输出前正则脱敏 `sk-*`、邮箱密码；`vault/reviews` 不入库 |
| 范围膨胀 | 每个里程碑有验收命令；不通过不进入下一个；§3"不做清单"写进 README 的已知限制 |

## 9. 已确认的决策（2026-11-17）

1. **邮件只读**（按最新版 README）：不实现发送、删除、移动，IMAP 只用 `BODY.PEEK`/`SEARCH`/`LIST`。`MailSender` 端口保留但默认不装配。
2. **LLM 用 pi 当前的 key 与模型**：`deepseek-flash`（DeepSeek V4.1 Flash）@ `https://ai-api.d-robotics.cc`，key 自动从 `~/.pi/agent/models.json` 读取（可用 `ASSISTANT_LLM_*` 覆盖）。已验证可调用、可返回 JSON。默认 `trust_env=False`，不继承 shell 的 socks 代理。
3. **先局域网 Web**，Android/iOS App 作为后续目标；因此 Web 层保持纯 HTTP + 表单，后续 App 直接复用 `/api/v1` 与同一套 service。
4. **安全默认（2026-11-17 追加）**：`your-assistant serve` 只监听 `127.0.0.1`；任何局域网监听必须显式 `--allow-lan` 或 `ASSISTANT_ALLOW_LAN=true`，且仍需 key 登录。未经用户允许不得暴露端口。

### 仍待你提供

- 邮箱地址 + 客户端专用密码（见对话中的清单）。
- 需要索引的资料目录（当前机器上没发现现成的笔记目录）。
- 几条初始重要性规则样例（谁/什么主题算重要）。
- 手机网络形态（校园 Wi-Fi 还是蜂窝），决定是否需要内网穿透文档。

## 10. 下一步

确认上述 3 点后，我按 M0 → M1 开始落地，**每完成一个里程碑就提交一次**（仓库当前 0 提交，M0 会先建立初始提交，提交信息用朴素中文描述，沿用你现有习惯，不加 emoji/Conventional 前缀）。