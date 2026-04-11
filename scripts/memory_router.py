"""
memory_router.py — 记忆路由器
核心职责：
  1. WAL 安全写入（Append-Only，消除并发脏写）
  2. L1 精确指针匹配
  3. L2 Topic 按需加载
  4. 语义向量召回（当 L1 找不到时的 fallback）
"""
import json
import re
import time
import datetime
from pathlib import Path
from typing import Optional

from config import (
    L1_INDEX_PATH, TOPICS_DIR, WAL_INBOX_PATH,
    EMBEDDINGS_DIR, TOPIC_MAX_TOKENS, TOPIC_CHARS_PER_TOKEN,
    SIMILARITY_THRESHOLD, EMBEDDING_DIM,
)


# ═══════════════════════════════════════════
# 1. WAL 预写日志（Append-Only 安全写入）
# ═══════════════════════════════════════════

# ── 安全扫描（借鉴 Hermes Agent 的 Prompt Injection 防护） ──

_INJECTION_PATTERNS = [
    # Prompt injection 常见模式
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"forget\s+(everything|all)\s+(you|about)", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
    # 凭证泄露模式
    re.compile(r"(api[_-]?key|secret|password|token)\s*[:=]\s*\S{8,}", re.IGNORECASE),
    re.compile(r"ssh-rsa\s+AAAA", re.IGNORECASE),
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----", re.IGNORECASE),
]

# 不可见 Unicode 字符（零宽字符等）
_INVISIBLE_CHARS = re.compile(r"[\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff]")


def _security_scan(content: str) -> str | None:
    """
    扫描记忆内容是否包含安全威胁。
    返回 None 表示安全，返回字符串表示被拦截的原因。
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(content):
            return f"安全拦截: 匹配到危险模式 [{pattern.pattern[:50]}...]"
    if _INVISIBLE_CHARS.search(content):
        return "安全拦截: 包含不可见 Unicode 字符（可能是隐蔽注入）"
    return None


def wal_append(topic: str, content: str, action: str = "add_fact",
               source: str = "unknown") -> dict:
    """
    将一条记忆以 Append-Only 的方式追加到 WAL Inbox。
    大模型在日常对话中 **绝不直接修改** L1_INDEX 或 Topic 文件，
    所有写入都通过此函数进入 Inbox，等 GC Worker 批量处理。
    """
    # 安全扫描
    threat = _security_scan(content)
    if threat:
        return {"blocked": True, "reason": threat, "content": content[:100]}

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "action": action,
        "topic": topic,
        "content": content,
        "source": source,
    }
    WAL_INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WAL_INBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def wal_read_all() -> list[dict]:
    """读取 WAL Inbox 中的全部未处理记录。"""
    if not WAL_INBOX_PATH.exists():
        return []
    entries = []
    with open(WAL_INBOX_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 跳过损坏的行（WAL 容错）
    return entries


def wal_clear():
    """清空 WAL Inbox（仅在 GC Commit 阶段调用）。"""
    with open(WAL_INBOX_PATH, "w", encoding="utf-8") as f:
        f.write("")


# ═══════════════════════════════════════════
# 2. L1 精确指针匹配
# ═══════════════════════════════════════════

def l1_load_index() -> str:
    """加载 L1 索引全文。"""
    if not L1_INDEX_PATH.exists():
        return ""
    return L1_INDEX_PATH.read_text(encoding="utf-8")


def l1_find_topic(query: str) -> Optional[Path]:
    """
    在 L1 索引中查找与 query 匹配的 Topic 指针。
    使用简单的关键词匹配：遍历索引中的每一行，
    检查 query 中的关键词是否命中行内容。
    返回命中的 Topic 文件路径，未命中返回 None。
    """
    index_text = l1_load_index()
    if not index_text:
        return None

    # 提取所有形如 → 见 topics/xxx.md 的指针
    pointer_pattern = re.compile(
        r"→\s*见\s*(topics/[\w\-]+\.md)", re.UNICODE
    )

    query_lower = query.lower()
    best_match = None
    best_score = 0

    for line in index_text.splitlines():
        line_lower = line.lower()
        # 计算 query 词汇在该行中命中的比例
        words = [w for w in re.split(r"[\s,;，；、]+", query_lower) if len(w) > 1]
        if not words:
            continue
        hits = sum(1 for w in words if w in line_lower)
        score = hits / len(words)

        if score > best_score:
            match = pointer_pattern.search(line)
            if match:
                best_score = score
                best_match = TOPICS_DIR.parent / match.group(1)

    if best_match and best_match.exists() and best_score > 0.3:
        return best_match
    return None


# ═══════════════════════════════════════════
# 3. L2 Topic 按需加载
# ═══════════════════════════════════════════

def l2_load_topic(topic_path: Path) -> str:
    """按需读取一个 Topic 文件的全文。"""
    if not topic_path.exists():
        return ""
    return topic_path.read_text(encoding="utf-8")


def l2_estimate_tokens(text: str) -> int:
    """粗略估算 Token 数（基于字符数 / 换算系数）。"""
    return int(len(text) / TOPIC_CHARS_PER_TOKEN)


def l2_list_topics() -> list[dict]:
    """列出 topics/ 下所有文件及其元信息。"""
    if not TOPICS_DIR.exists():
        return []
    result = []
    for p in sorted(TOPICS_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        result.append({
            "name": p.stem,
            "file": f"topics/{p.name}",
            "size_bytes": p.stat().st_size,
            "est_tokens": l2_estimate_tokens(text),
            "last_modified": datetime.datetime.fromtimestamp(
                p.stat().st_mtime
            ).isoformat(),
        })
    return result


# ═══════════════════════════════════════════
# 4. 语义向量召回（RAG Fallback）
# ═══════════════════════════════════════════

class SimpleVectorStore:
    """
    轻量级的本地向量存储。
    不依赖 ChromaDB/Faiss 等重型库，使用纯 Python + numpy 实现。
    适合 Topic 文件数量 < 500 的场景。
    """

    def __init__(self, store_path: Path = EMBEDDINGS_DIR / "vectors.json"):
        self.store_path = store_path
        self.vectors: dict[str, list[float]] = {}
        self._load()

    def _load(self):
        if self.store_path.exists():
            with open(self.store_path, "r", encoding="utf-8") as f:
                self.vectors = json.load(f)

    def _save(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(self.vectors, f, ensure_ascii=False)

    def upsert(self, topic_name: str, embedding: list[float]):
        """插入或更新一个 Topic 的向量表示。"""
        self.vectors[topic_name] = embedding
        self._save()

    def search(self, query_embedding: list[float], top_k: int = 3) -> list[tuple[str, float]]:
        """
        余弦相似度搜索。返回 [(topic_name, similarity_score), ...]
        """
        if not self.vectors:
            return []

        results = []
        for name, vec in self.vectors.items():
            sim = self._cosine_similarity(query_embedding, vec)
            if sim >= SIMILARITY_THRESHOLD:
                results.append((name, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x ** 2 for x in a) ** 0.5
        norm_b = sum(x ** 2 for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def get_embedding(text: str) -> list[float]:
    """
    获取文本的 Embedding 向量。
    默认实现：返回零向量（纯数据层不依赖外部 API）。
    如需启用语义检索，由调用方 Agent 自行提供 embedding 函数。
    """
    # 纯数据层默认：返回零向量（功能降级但不崩溃）
    return [0.0] * EMBEDDING_DIM


# ═══════════════════════════════════════════
# 5. 统一路由入口
# ═══════════════════════════════════════════

def route_query(query: str) -> dict:
    """
    统一的记忆路由入口。双轨召回：
      1. 先尝试 L1 精确指针匹配
      2. 未命中则 fallback 到语义向量检索
    
    返回:
      {
        "hit": True/False,
        "method": "l1_pointer" | "rag_vector" | "none",
        "topic_name": "xxx",
        "topic_content": "...",
      }
    """
    # ── 第一轨：L1 精确匹配 ──
    topic_path = l1_find_topic(query)
    if topic_path:
        return {
            "hit": True,
            "method": "l1_pointer",
            "topic_name": topic_path.stem,
            "topic_content": l2_load_topic(topic_path),
        }

    # ── 第二轨：RAG 语义召回 ──
    try:
        query_vec = get_embedding(query)
        store = SimpleVectorStore()
        results = store.search(query_vec, top_k=1)
        if results:
            topic_name, score = results[0]
            topic_path = TOPICS_DIR / f"{topic_name}.md"
            if topic_path.exists():
                return {
                    "hit": True,
                    "method": "rag_vector",
                    "topic_name": topic_name,
                    "topic_content": l2_load_topic(topic_path),
                    "similarity": round(score, 4),
                }
    except Exception:
        pass  # RAG 不可用时静默降级

    return {"hit": False, "method": "none", "topic_name": None, "topic_content": None}


# ═══════════════════════════════════════════
# 6. 会话计数器（用于三重门的会话门）
# ═══════════════════════════════════════════

def session_increment():
    """每次新会话启动时调用，递增计数器。"""
    from config import SESSION_COUNTER_PATH
    count = 0
    if SESSION_COUNTER_PATH.exists():
        try:
            count = int(SESSION_COUNTER_PATH.read_text().strip())
        except ValueError:
            count = 0
    count += 1
    SESSION_COUNTER_PATH.write_text(str(count), encoding="utf-8")
    return count


def session_reset():
    """Dream 整理完成后重置计数器。"""
    from config import SESSION_COUNTER_PATH
    SESSION_COUNTER_PATH.write_text("0", encoding="utf-8")


if __name__ == "__main__":
    # 快速测试
    print("=== Memory Router 快速诊断 ===")
    print(f"L1 索引路径: {L1_INDEX_PATH}")
    print(f"L1 索引存在: {L1_INDEX_PATH.exists()}")
    print(f"WAL Inbox 路径: {WAL_INBOX_PATH}")
    print(f"WAL 积压记录数: {len(wal_read_all())}")
    print(f"Topic 文件列表:")
    for t in l2_list_topics():
        print(f"  - {t['name']} ({t['est_tokens']} tokens, 更新于 {t['last_modified']})")

    # 测试 WAL 写入
    entry = wal_append(
        topic="test_topic",
        content="这是一条测试记忆",
        source="router_self_test",
    )
    print(f"\n测试 WAL 写入成功: {entry}")
