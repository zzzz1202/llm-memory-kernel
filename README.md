# llm-memory-kernel

为无状态大语言模型提供外挂式持久化记忆内核。

## 架构

```
llm-memory-kernel/
├── memory/
│   ├── L1_INDEX.md          # L1 缓存：全局索引（≤200 行，每次会话必读）
│   ├── topics/              # L2 缓存：按需加载的主题文件
│   └── archive/             # 冷存储：被 GC 归档的过期数据
├── system/
│   ├── wal_inbox.jsonl      # 预写日志（Append-Only，消除并发脏写）
│   └── embeddings/          # 本地向量库（用于 RAG 语义召回）
├── core_prompts/
│   ├── INGESTION.md         # 日常对话 → 记忆提取
│   ├── MAP_COMPACT.md       # 单 Topic 微压缩（Map Phase）
│   ├── REDUCE_INDEX.md      # 全局索引重建（Reduce Phase）
│   ├── DREAM_PROTOCOL.md    # AutoDream 五阶段整理协议
│   ├── TOPIC_TEMPLATE.md    # 主题文件模板
│   └── ERRORS_TEMPLATE.md   # 错误自愈模板
├── scripts/
│   ├── config.py            # 全局配置（阈值、路径、API）
│   ├── memory_router.py     # 路由器：WAL 写入 + L1/L2 匹配 + RAG 召回
│   └── gc_worker.py         # GC：Map-Reduce 异步垃圾回收
└── backups/                 # 自动备份（保留最近 4 个）
```

## 核心设计

| 机制 | 说明 |
|------|------|
| **WAL 预写日志** | 大模型日常对话中绝不直接修改索引或 Topic 文件，全部 Append-Only 写入 Inbox |
| **双轨召回** | L1 精确指针匹配 + RAG 语义向量搜索（L1 未命中时 fallback） |
| **Map-Reduce GC** | 分治执行：Map（每个 Topic 独立压缩）→ Reduce（重建全局索引）→ Commit（清空 Inbox） |
| **五阶段 Dream** | 定向 → 收集 → 整合 → 老化检测 → 剪枝重建 |
| **三重门触发** | 时间门（≥24h）+ 会话门（≥5次）+ 锁门（防并发），三门全开才触发 |
| **怀疑式记忆** | 记忆视为"提示"而非"事实"，执行前交叉验证 |
| **错误自愈** | 被纠正时强制记录根因，输出方案前强制查阅错误记录 |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置 LLM API（用于 GC 的自动压缩，可选）
export LMK_LLM_API_KEY="your-api-key"
export LMK_LLM_MODEL="gpt-4o-mini"  # 或 deepseek-chat 等

# 路由器诊断
python scripts/memory_router.py

# 检查三重门状态
python scripts/gc_worker.py --check

# 手动执行 Dream 整理
python scripts/gc_worker.py --force

# 手动备份
python scripts/gc_worker.py --backup
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LMK_ROOT` | 仓库根目录 | 覆盖记忆文件的存储根路径 |
| `LMK_LLM_API_BASE` | `https://api.openai.com/v1` | LLM API 地址 |
| `LMK_LLM_API_KEY` | (空) | LLM API 密钥 |
| `LMK_LLM_MODEL` | `gpt-4o-mini` | 用于 GC 自动压缩的模型 |
| `LMK_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | Embedding 模型 |

## License

MIT
