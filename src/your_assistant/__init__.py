"""会成长的个人助手：只读邮件 + 可审阅记忆 + 需要确认的操作。

模块职责（刻意保持扁平，见 PLAN.md）：
- config   环境配置与能力开关
- models   领域对象
- ports    所有对外接缝（Protocol）
- db       SQLite 连接与迁移
- cli      命令行入口
"""

__version__ = "0.1.0"
