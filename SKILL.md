---
name: llm-memory-kernel
description: 为无状态大语言模型提供外挂式持久化记忆内核。三层缓存架构（L1 索引 → L2 主题文件 → L3 归档），WAL 预写日志保障数据一致性，Map-Reduce GC 自动整理，RAG 向量召回兜底检索。融合 Karpathy Wiki Pattern：支持原始素材摄入、Lint 健康检查、人类可读时间线日志。
---

# LLM Memory Kernel — 持久化记忆技能

> 本技能为 AI 提供跨会话的持久化记忆能力。所有记忆操作通过本技能的标准流程执行，严格禁止直接修改记忆文件。

## 核心原则

### 怀疑式记忆
- **记忆是提示，不是事实**。从记忆中召回的信息必须视为"可能过时的线索"
- 执行关键操作前，必须与用户确认记忆中的假设是否仍然成立
- 发现记忆与用户当前陈述矛盾时，以用户当前陈述为准

### Write-Ahead Logging (WAL)
- **绝不直接修改** `memory/L1_INDEX.md` 或 `memory/topics/*.md`
- 所有新记忆通过 WAL 安全写入 `system/wal_inbox.jsonl`，等待 GC 批量处理
- 写入方式：调用 `python scripts/memory_router.py` 或在代码中调用 `wal_append()` 函数

## 架构概览

```
memory/
├── L1_INDEX.md          # L1：全局索引（≤200行），每次会话启动时加载
├── topics/              # L2：按主题分文件存储，按需加载
└── archive/             # L3：被 GC 归档的过期数据（冷存储）

system/
├── wal_inbox.jsonl      # WAL 预写日志（Append-Only）
├── embeddings/          # RAG 向量索引
├── .dream_lock          # Dream 整理锁（防并发）
└── session_counter      # 会话计数器（用于触发 Dream）

core_prompts/            # Prompt 模板
├── INGESTION.md         # 日常对话 → 记忆提取
├── INGEST_SOURCE.md     # 原始素材 → 知识提取
├── LINT.md              # 健康检查规则（死链、孤儿、交叉引用）
├── MAP_COMPACT.md       # Topic 合并规则（Map Phase）
├── REDUCE_INDEX.md      # 索引重建规则（Reduce Phase）
├── DREAM_PROTOCOL.md    # AutoDream 六阶段整理协议
├── TOPIC_TEMPLATE.md    # 新 Topic 文件模板
└── ERRORS_TEMPLATE.md   # 错误自愈模板

raw/                     # 原始素材（刪文章、笔记等，不可变）

scripts/                 # Python 引擎
├── config.py            # 全局配置（路径、阈值、API）
├── memory_router.py     # 路由器：WAL 写入 + L1/L2 匹配 + RAG 向量召回
├── gc_worker.py         # GC Worker：三重门检查 + Map-Reduce 整理 + Lint + 备份
└── ingest.py            # 素材摄入引擎：读取 raw/ 文件 → 提取关键信息 → 写入 WAL
```

## 会话生命周期

### 1. 会话启动
```
加载 memory/L1_INDEX.md → 获取全局记忆概览
递增 system/session_counter（用于触发 Dream 的会话门）
```

### 2. 日常对话中的记忆操作

**读取记忆（双轨召回）：**
1. 先在 L1 索引中做关键词匹配，找到对应的 Topic 文件指针
2. 如果 L1 未命中，fallback 到 RAG 语义向量检索
3. 按需加载命中的 Topic 文件（L2）

**写入记忆：**
1. 参照 `core_prompts/INGESTION.md` 的规则，提取对话中的持久化信息
2. 以 JSONL 格式追加写入 `system/wal_inbox.jsonl`
3. **绝不**直接修改 L1_INDEX 或 Topic 文件

**被用户纠正时：**
1. 立即记录到 WAL（action: "correction"）
2. 参照 `core_prompts/ERRORS_TEMPLATE.md` 的格式填写错误记录
3. 后续输出复杂方案前，先查阅错误记录中的自检清单

### 3. 会话结束 / Dream 整理
当满足三重门条件（时间门 ≥24h + 会话门 ≥5次 + 锁门空闲）或用户输入 `/dream` 时：

```bash
# 检查是否需要整理
python scripts/gc_worker.py --check

# 执行整理（自动模式，需三重门全开）
python scripts/gc_worker.py

# 强制整理（忽略三重门）
python scripts/gc_worker.py --force

# 手动备份
python scripts/gc_worker.py --backup
```

Dream 六阶段：定向→收集→整合(Map)→老化检测→Lint健康检查→剪枝重建(Reduce)

## 素材摄入 (Ingest)

与对话中自动提取不同，Ingest 是主动向记忆系统“喂”外部素材（文章、笔记、书摘等）。

```bash
# 处理单个文件
python scripts/ingest.py raw/文件名.md

# 列出 raw/ 下的文件
python scripts/ingest.py --list
```

流程：读取原文 → 写入 WAL → 等待 Dream 整合。

## 记忆写入格式

通过 WAL 写入的每条记录格式：
```jsonl
{"timestamp": "ISO-8601", "action": "add_fact|correction|preference_update", "topic": "snake_case_topic_name", "content": "精简客观描述", "source": "chat_turn_N"}
```

## 关键阈值

| 参数 | 默认值 | 说明 |
|------|--------|------|
| L1 索引上限 | 200 行 | 超出时 Reduce Phase 自动溢出 |
| Topic 压缩阈值 | 3000 Tokens | 超出时 Map Phase 自动压缩 |
| 时间门 | 24 小时 | Dream 触发间隔 |
| 会话门 | 5 次 | Dream 触发会话数 |
| 老化警告 | 30 天 | 未访问的 Topic 标记为老化 |
| 老化归档 | 60 天 | 未访问的 Topic 移入 archive/ |
| 备份保留 | 4 个 | 超出自动清理旧备份 |

## 环境变量（可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LMK_ROOT` | 本技能根目录 | 覆盖存储根路径 |

## 注意事项

- 本系统是**纯数据层**，不依赖任何外部 LLM API。智能由调用方 Agent 自带。
- `memory/` 目录存储用户的持久化数据，**请勿随意删除**
- `system/wal_inbox.jsonl` 是 Append-Only 的缓冲区，只有 GC Worker 有权清空
- 如果 `.dream_lock` 文件存在超过 1 小时，系统会视为死锁并自动清除

