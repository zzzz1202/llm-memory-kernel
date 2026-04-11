# 集成指南：如何将 LLM Memory Kernel 接入你的 Agent

> 本指南适用于任何 LLM Agent（Claude Code、OpenClaw、Cursor、Dify、自建 Bot 等）。
> 记忆内核是**纯数据层**，不依赖任何外部 LLM API。你的 Agent 自带智能。

---

## 快速开始（3 步）

### 1. 克隆并初始化

```bash
git clone https://github.com/zzzz1202/llm-memory-kernel.git
cd llm-memory-kernel
python scripts/init.py
```

这会创建你的私人记忆空间（Topic 文件、索引、日志）。这些数据不会进入 Git。

### 2. 让 Agent 读取记忆

在你的 Agent 启动时，读取以下文件：

```
memory/L1_INDEX.md    ← 全局记忆索引（≤200 行，秒读）
```

如果需要某个主题的详细内容，按需读取：

```
memory/topics/user_profile.md
memory/topics/preferences.md
```

### 3. 让 Agent 写入记忆

当对话中出现值得长期保存的信息时，Agent 写入 WAL：

```python
import sys
sys.path.append("/path/to/llm-memory-kernel/scripts")
from memory_router import wal_append

wal_append(
    topic="user_profile",          # 归属主题
    content="用户偏好使用异步 API",   # 精简客观描述
    action="add_fact",             # add_fact | correction | preference_update | discovery | milestone
    source="agent_name",           # 来源标识
)
```

---

## 记忆整理

WAL 是临时缓冲区。定期运行整理，将 WAL 内容合并进 Topic 文件：

```bash
python scripts/gc_worker.py --force
```

整理会自动完成：
- ✅ WAL 内容合并到对应 Topic 文件
- ✅ L1 索引重建
- ✅ 老化检测（60 天未访问 → 归档）
- ✅ Lint 健康检查（死链、孤儿、交叉引用）
- ✅ 自动备份
- ✅ 追加 log.md 时间线

---

## 集成模式

### 模式 A：Agent 直接调用 Python（推荐）

适用于有 `run_command` 或 `code_execution` 能力的 Agent。

```python
from memory_router import wal_append, route_query

# 写入
wal_append(topic="preferences", content="...", source="my_agent")

# 查询
result = route_query("用户的技术偏好")
if result["hit"]:
    print(result["topic_content"])
```

### 模式 B：Agent 直接读写文件

适用于只有文件读写能力的 Agent（如 Claude Code、Cursor）。

**写入 WAL**：向 `system/wal_inbox.jsonl` 追加一行 JSON：

```jsonl
{"timestamp": "2026-04-11T01:00:00", "action": "add_fact", "topic": "user_profile", "content": "用户在做 AI 教育", "source": "claude_code"}
```

**读取记忆**：直接读 `memory/L1_INDEX.md` 和 `memory/topics/*.md`。

### 模式 C：注入 System Prompt

如果你的 Agent 不支持文件操作，可以直接把 `SKILL.md` 的内容复制进 System Prompt，并在每次会话开始时手动粘贴 `L1_INDEX.md` 的内容。

---

## 多 Agent 共享

多个 Agent 可以同时接入同一个记忆内核：

```
Antigravity ──写──→ wal_inbox.jsonl ←──写── OpenClaw
                         ↓
                   gc_worker.py（整理）
                         ↓
               topics/*.md + L1_INDEX.md
                    ↑          ↑
        Antigravity 读     OpenClaw 读
```

**安全性**：WAL 是 append-only 的 JSONL 文件，多 Agent 并发写入不会互相覆盖。

---

## 目录结构说明

```
llm-memory-kernel/
├── scripts/           # 引擎（可分享）
│   ├── init.py        # 一键初始化
│   ├── config.py      # 全局配置
│   ├── memory_router.py  # 读写路由
│   ├── gc_worker.py   # 整理器
│   └── ingest.py      # 素材摄入
├── core_prompts/      # 协议模板（可分享）
├── raw/               # 原始素材（私人）
├── memory/            # 记忆数据（私人，自动生成）
│   ├── L1_INDEX.md
│   ├── log.md
│   ├── topics/
│   └── archive/
├── system/            # 系统文件（私人，自动生成）
│   └── wal_inbox.jsonl
├── SKILL.md           # Agent 操作协议
├── README.md          # 项目说明
└── INTEGRATION.md     # 本文件
```

**引擎**（进入 Git，可分享）：`scripts/`、`core_prompts/`、`SKILL.md`、`README.md`
**数据**（不进入 Git，私人）：`memory/`、`system/`、`raw/`、`backups/`

---

## 零依赖

本系统是**纯 Python + Markdown**，不需要安装任何第三方库。

- 不需要 `openai`
- 不需要 `numpy`
- 不需要数据库
- 不需要 Docker

Python 3.9+ 即可运行。
