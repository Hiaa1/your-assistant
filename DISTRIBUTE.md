# 分发与部署

这份文件回答两件事：第二个人怎么在你电脑之外跑起来，以及哪些东西绝不能进 Git。

## 仓库里有什么

| 类型 | 路径 | 说明 |
| --- | --- | --- |
| 代码 | `src/`、`tests/`、`pyproject.toml`、`uv.lock`、`.github/` | 软件本体，可直接分发 |
| 模板 | `.env.example`、`rules/` | 配置和规则模板，可进 Git |
| 素材 | `src/your_assistant/static/emoji/`、`icon.svg` | 像素图标与前端资源 |
| 文档 | `README.md`、`HANDOVER.md`、`DISTRIBUTE.md` | 使用与交接说明 |
| **个人内容** | `.env`、`data/`、`vault/`、`memory/` | 邮箱密码、数据库、笔记、复盘、纠错案例，**不进 Git** |

判断标准：换一个人、换一台机器，这东西还有意义 → 代码/模板；只对你有意义 → 个人内容。

## 第二个人怎么跑起来

前提：macOS / Linux，能装 `uv`。

```bash
git clone <仓库地址> your-assistant
cd your-assistant
brew install uv          # 或 curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync --locked --group dev

cp .env.example .env
chmod 600 .env
# 填 ASSISTANT_EMAIL_ADDRESS / ASSISTANT_EMAIL_PASSWORD / ASSISTANT_PASSWORD / ASSISTANT_SESSION_SECRET

uv run your-assistant doctor --net   # 验证 IMAP 与 LLM
uv run your-assistant index
uv run your-assistant sync
uv run your-assistant serve          # 默认 http://127.0.0.1:8000
```

手机访问要显式授权：`uv run your-assistant serve --allow-lan`，并在 macOS 放行本地网络。

### 个人数据默认在哪

都在项目目录里，且已被 `.gitignore` 排除：

```
.env          邮箱密码、Web 密码、session secret
data/         SQLite、邮件缓存、日志
vault/        笔记、博客、每日复盘
memory/       纠错案例
```

想放到别处（比如 `~/your-assistant-data/`），在 `.env` 里改：

```bash
ASSISTANT_DATA_DIR=~/your-assistant-data/data
ASSISTANT_VAULT_DIR=~/your-assistant-data/vault
ASSISTANT_MEMORY_DIR=~/your-assistant-data/memory
ASSISTANT_RULES_FILE=~/your-assistant-data/rules/lessons.md
ASSISTANT_MAIL_RULES_FILE=~/your-assistant-data/rules/mail_rules.yaml
```

## 自己创建一个 Git 仓库

1. 在 GitHub / GitLab 新建**空仓库**（不要勾选初始化 README、.gitignore、license）。
2. 本地加 remote 并推：

```bash
cd /path/to/personal_agent
git remote add origin git@github.com:<你的账号>/<仓库名>.git
git push -u origin main
```

3. 之后改动：

```bash
git add -A
git commit -m "一句话说明改了什么"
git push
```

### 如果你要把仓库公开

先检查三件事：

```bash
git ls-files | grep -E '^\.env$|^data/|^vault/|^memory/'   # 应该没有输出
git log -p -- .env | head                                    # 应该没有历史
git grep -n "密码\|password\|secret" -- . ':!*.lock' | head   # 人工过一遍
```

`.env`、`data/`、`vault/`、`memory/` 一旦进过 Git，即使后来删除，历史里仍可恢复，必须用 `git filter-repo` 清理。

## 别人更新你的代码

```bash
git pull
uv sync --locked --group dev
```

数据库结构变化会自动跑迁移；个人内容不会被动。

## 不分发的东西

- 邮箱客户端专用密码（每人自己的）
- `data/assistant.db`（绝对路径、邮件缓存）
- `vault/`（私人笔记、博客草稿、复盘）
- `memory/cases/`（个人纠错记录）
- 生成的 `.env` 与任何 `ASSISTANT_*_SECRET`
