# LLM Memory Kernel

> 为无状态 AI Agent 提供持久化记忆的外挂式内核。
> 纯数据层，零外部依赖，任何 Agent 即插即用。

## 它解决什么问题？

LLM 是无状态的——每次新会话，它都会忘掉你是谁、你的偏好、你上次犯的错。

本项目提供一个 **文件系统级别的记忆层**，让任何 AI Agent（Claude Code、OpenClaw、Cursor、Dify 等）都能在对话之间保持记忆连续性。

**灵感来源**：[Andrej Karpathy 的 LLM Wiki Pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，并融合了范凯的工程化改造理念。

## 核心特性

- 🧠 **三层缓存**：L1 索引（秒读）→ L2 Topic 文件（按需加载）→ L3 归档（冷存储）
- 📝 **WAL 预写日志**：所有写入先进 WAL，确保并发安全和数据一致性
- 🔄 **自动整理**：六阶段 Dream 协议（收集 → 合并 → 老化 → Lint → 重建 → 备份）
- 🔍 **Lint 健康检查**：自动检测死链、孤儿页、信息空洞、缺失交叉引用
- 📊 **人类可读日志**：`log.md` 记录每次操作，可用 grep 过滤
- 📥 **素材摄入**：支持将外部文件批量导入记忆系统
- 🤝 **多 Agent 共享**：多个 Agent 可同时读写同一份记忆
- 🚫 **零依赖**：纯 Python + Markdown，不需要 openai、数据库或 Docker

## 架构

```
┌─────────────────────────────────────────────┐
│  你的 Agent（自带 LLM）                      │
│  Claude Code / OpenClaw / Cursor / ...      │
└──────────┬──────────────────┬───────────────┘
           │ 写入 WAL          │ 读取 Topics
           ▼                  ▼
┌─────────────────────────────────────────────┐
│  LLM Memory Kernel（纯数据层）               │
│                                             │
│  system/wal_inbox.jsonl  ← 预写日志          │
│           ↓                                 │
│  gc_worker.py（定期整理）                    │
│           ↓                                 │
│  memory/L1_INDEX.md     ← 全局索引（≤200行） │
│  memory/topics/*.md     ← 主题文件           │
│  memory/log.md          ← 操作时间线         │
│  memory/archive/        ← 冷存储             │
└─────────────────────────────────────────────┘
```

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/zzzz1202/llm-memory-kernel.git
cd llm-memory-kernel

# 2. 初始化（创建你的私人记忆空间）
python scripts/init.py

# 3. 让你的 Agent 开始使用
#    详见 INTEGRATION.md
```

## Agent 集成（3 种模式）

### 模式 A：Python 调用（推荐）

```python
import sys
sys.path.append("/path/to/llm-memory-kernel/scripts")
from memory_router import wal_append, route_query

# 写入记忆
wal_append(topic="user_profile", content="用户偏好异步 API", source="my_agent")

# 查询记忆
result = route_query("用户的技术偏好")
```

### 模式 B：直接读写文件

向 `system/wal_inbox.jsonl` 追加一行 JSON 即可写入：

```jsonl
{"timestamp": "2026-04-11T01:00:00", "action": "add_fact", "topic": "preferences", "content": "喜欢简洁代码", "source": "agent"}
```

### 模式 C：System Prompt 注入

将 `SKILL.md` 复制进 Agent 的系统提示词。详见 [INTEGRATION.md](INTEGRATION.md)。

## 记忆整理

```bash
# 检查系统状态
python scripts/gc_worker.py --check

# 执行整理（WAL 合并 + Lint + 索引重建 + 备份）
python scripts/gc_worker.py --force

# 摄入外部素材
python scripts/ingest.py raw/文件名.md
```

## 文件结构

```
llm-memory-kernel/
├── scripts/               # 引擎代码
│   ├── init.py            # 一键初始化
│   ├── config.py          # 全局配置
│   ├── memory_router.py   # 读写路由（WAL + L1/L2 匹配）
│   ├── gc_worker.py       # 整理器（六阶段 Dream 协议）
│   └── ingest.py          # 素材摄入
├── core_prompts/          # 协议模板
│   ├── INGESTION.md       # 对话 → 记忆提取规则
│   ├── INGEST_SOURCE.md   # 素材 → 知识提取规则
│   ├── LINT.md            # 健康检查规则
│   ├── MAP_COMPACT.md     # Topic 合并规则
│   ├── REDUCE_INDEX.md    # 索引重建规则
│   └── DREAM_PROTOCOL.md  # Dream 六阶段协议
├── SKILL.md               # Agent 操作手册
├── INTEGRATION.md         # 集成指南
└── README.md              # 本文件
```

## 设计原则

1. **纯数据层**：记忆系统只做存储和整理，不调用外部 LLM。智能由 Agent 自带。
2. **怀疑式记忆**：记忆是提示，不是事实。Agent 在执行关键操作前必须与用户确认。
3. **WAL 优先**：所有写入通过预写日志，永不直接修改 Topic 文件。
4. **零依赖**：Python 3.9+ 即可运行，不需要安装任何第三方包。
5. **引擎与数据分离**：仓库只包含引擎代码，私人记忆通过 `init.py` 本地生成。

## 致谢

- [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — Wiki Pattern 理念
- 范凯（范凯说AI）— 工程化改造方案（确认机制、领域隔离、对话萃取）

## License

MIT
