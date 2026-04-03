"""
llm-memory-kernel 全局配置
"""
import os
from pathlib import Path

# ── 根路径（默认为本仓库所在目录，可通过环境变量覆盖） ──
KERNEL_ROOT = Path(os.environ.get("LMK_ROOT", Path(__file__).resolve().parent.parent))

# ── 存储路径 ──
MEMORY_DIR = KERNEL_ROOT / "memory"
L1_INDEX_PATH = MEMORY_DIR / "L1_INDEX.md"
TOPICS_DIR = MEMORY_DIR / "topics"
ARCHIVE_DIR = MEMORY_DIR / "archive"

SYSTEM_DIR = KERNEL_ROOT / "system"
WAL_INBOX_PATH = SYSTEM_DIR / "wal_inbox.jsonl"
EMBEDDINGS_DIR = SYSTEM_DIR / "embeddings"
DREAM_LOCK_PATH = SYSTEM_DIR / ".dream_lock"
SESSION_COUNTER_PATH = SYSTEM_DIR / "session_counter"

PROMPTS_DIR = KERNEL_ROOT / "core_prompts"

# ── 阈值与限制 ──
L1_MAX_LINES = 200           # L1 索引最大行数
L1_MAX_LINE_LENGTH = 150     # L1 每行最大字符数
TOPIC_MAX_TOKENS = 3000      # 单个 Topic 文件触发微压缩的阈值
TOPIC_CHARS_PER_TOKEN = 1.5  # 中文场景下的粗略换算（1 token ≈ 1.5 汉字）

# ── AutoDream 触发阈值 ──
DREAM_TIME_GATE_HOURS = 24   # 时间门：距上次整理 ≥ N 小时
DREAM_SESSION_GATE = 5       # 会话门：距上次整理 ≥ N 次会话
DREAM_LOCK_TIMEOUT_HOURS = 1 # 锁门：超过 N 小时视为死锁

# ── 老化策略 ──
AGING_WARN_DAYS = 30         # 30 天未访问标记为老化
AGING_ARCHIVE_DAYS = 60      # 60 天未访问提升为归档候选

# ── 备份 ──
BACKUP_DIR = KERNEL_ROOT / "backups"
BACKUP_RETENTION_COUNT = 4   # 最多保留最近 N 个备份（解决无限增长问题）

# ── RAG / Embedding 配置 ──
EMBEDDING_MODEL = os.environ.get("LMK_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DIM = 512
SIMILARITY_THRESHOLD = 0.65  # 余弦相似度召回阈值

# ── LLM API（用于 GC Worker 自动调用大模型执行压缩） ──
LLM_API_BASE = os.environ.get("LMK_LLM_API_BASE", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("LMK_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LMK_LLM_MODEL", "gpt-4o-mini")
