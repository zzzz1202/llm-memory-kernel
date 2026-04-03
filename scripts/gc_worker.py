"""
gc_worker.py — 异步垃圾回收器 (Map-Reduce GC)
核心职责：
  1. 检查三重门触发条件
  2. Map Phase：按 Topic 分组处理 WAL Inbox，调用 LLM 执行微压缩
  3. Reduce Phase：重建 L1 索引
  4. Commit：清空 Inbox，归档老化数据，释放锁
"""
import json
import shutil
import datetime
from pathlib import Path
from collections import defaultdict
from typing import Optional

from config import (
    L1_INDEX_PATH, TOPICS_DIR, ARCHIVE_DIR, WAL_INBOX_PATH,
    DREAM_LOCK_PATH, SESSION_COUNTER_PATH, PROMPTS_DIR,
    BACKUP_DIR, BACKUP_RETENTION_COUNT,
    DREAM_TIME_GATE_HOURS, DREAM_SESSION_GATE, DREAM_LOCK_TIMEOUT_HOURS,
    AGING_WARN_DAYS, AGING_ARCHIVE_DAYS, L1_MAX_LINES, L1_MAX_LINE_LENGTH,
    TOPIC_MAX_TOKENS, TOPIC_CHARS_PER_TOKEN,
    LLM_API_BASE, LLM_API_KEY, LLM_MODEL,
)
from memory_router import (
    wal_read_all, wal_clear, l1_load_index, l2_list_topics,
    l2_load_topic, l2_estimate_tokens, session_reset,
)


# ═══════════════════════════════════════════
# 1. 三重门检查
# ═══════════════════════════════════════════

def check_time_gate() -> bool:
    """时间门：距上次 Dream ≥ N 小时。"""
    index_text = l1_load_index()
    for line in reversed(index_text.splitlines()):
        if "最后一次 Dream 整理时间" in line:
            # 尝试解析时间戳
            import re
            match = re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})", line)
            if match:
                try:
                    last_dream = datetime.datetime.fromisoformat(match.group(1))
                    elapsed = datetime.datetime.now() - last_dream
                    return elapsed.total_seconds() >= DREAM_TIME_GATE_HOURS * 3600
                except ValueError:
                    pass
    return True  # 从未整理过，门打开


def check_session_gate() -> bool:
    """会话门：距上次整理 ≥ N 次会话。"""
    if not SESSION_COUNTER_PATH.exists():
        return False
    try:
        count = int(SESSION_COUNTER_PATH.read_text().strip())
        return count >= DREAM_SESSION_GATE
    except ValueError:
        return False


def check_lock_gate() -> bool:
    """
    锁门：.dream_lock 不存在则门打开。
    如果存在但超过超时时间，视为死锁并强制清除。
    """
    if not DREAM_LOCK_PATH.exists():
        return True
    # 检查是否超时
    lock_mtime = datetime.datetime.fromtimestamp(DREAM_LOCK_PATH.stat().st_mtime)
    elapsed = datetime.datetime.now() - lock_mtime
    if elapsed.total_seconds() >= DREAM_LOCK_TIMEOUT_HOURS * 3600:
        print(f"[GC] 检测到死锁（锁已存在 {elapsed}），强制清除")
        DREAM_LOCK_PATH.unlink()
        return True
    return False


def should_dream() -> tuple[bool, str]:
    """综合检查三重门。返回 (是否触发, 原因说明)。"""
    time_ok = check_time_gate()
    session_ok = check_session_gate()
    lock_ok = check_lock_gate()

    if not lock_ok:
        return False, "锁门关闭：另一个 Dream 进程正在运行"
    if time_ok and session_ok:
        return True, f"三重门全开（时间门={time_ok}, 会话门={session_ok}）"
    reasons = []
    if not time_ok:
        reasons.append("时间门未满足")
    if not session_ok:
        reasons.append("会话门未满足")
    return False, "；".join(reasons)


# ═══════════════════════════════════════════
# 2. 锁管理
# ═══════════════════════════════════════════

def acquire_lock(stage: str = "starting"):
    """获取 Dream 锁，写入当前阶段信息。"""
    DREAM_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_info = {
        "locked_at": datetime.datetime.now().isoformat(),
        "stage": stage,
        "pid": __import__("os").getpid(),
    }
    DREAM_LOCK_PATH.write_text(json.dumps(lock_info, ensure_ascii=False), encoding="utf-8")


def update_lock_stage(stage: str):
    """更新锁中的阶段信息（用于中断恢复）。"""
    if DREAM_LOCK_PATH.exists():
        lock_info = json.loads(DREAM_LOCK_PATH.read_text(encoding="utf-8"))
        lock_info["stage"] = stage
        DREAM_LOCK_PATH.write_text(json.dumps(lock_info, ensure_ascii=False), encoding="utf-8")


def release_lock():
    """释放 Dream 锁。"""
    if DREAM_LOCK_PATH.exists():
        DREAM_LOCK_PATH.unlink()


# ═══════════════════════════════════════════
# 3. LLM 调用封装
# ═══════════════════════════════════════════

def call_llm(system_prompt: str, user_prompt: str) -> str:
    """
    调用 LLM API 执行合并/压缩任务。
    兼容 OpenAI API 格式（可指向 OpenAI/DeepSeek/本地 Ollama 等）。
    """
    try:
        import openai
        client = openai.OpenAI(base_url=LLM_API_BASE, api_key=LLM_API_KEY)
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
        )
        return resp.choices[0].message.content
    except Exception as e:
        print(f"[GC] LLM 调用失败: {e}")
        return ""


# ═══════════════════════════════════════════
# 4. Map Phase：按 Topic 微压缩
# ═══════════════════════════════════════════

def map_compact(topic_name: str, existing_content: str, new_entries: list[dict]) -> str:
    """
    对单个 Topic 执行 Map 阶段的微压缩。
    读取 core_prompts/MAP_COMPACT.md 作为 system prompt，
    将现有内容 + 新日志作为 user prompt 发送给 LLM。
    """
    # 加载 Map Compact prompt
    prompt_path = PROMPTS_DIR / "MAP_COMPACT.md"
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else (
        "你是数据 Compactor。将输入 A（旧状态）和输入 B（新日志）合并，"
        "以新日志为准解决矛盾，压缩冗余，输出结构化 Markdown。"
    )

    entries_text = "\n".join(
        f"- [{e.get('timestamp', '?')}] {e.get('content', '')}" for e in new_entries
    )
    user_prompt = (
        f"## 输入 A：现有状态\n```\n{existing_content}\n```\n\n"
        f"## 输入 B：新增日志\n{entries_text}\n\n"
        f"请执行合并与微压缩，主题名: {topic_name}"
    )

    result = call_llm(system_prompt, user_prompt)
    if not result:
        # LLM 不可用时 fallback：简单追加
        return existing_content + "\n\n## 新增（待压缩）\n" + entries_text
    return result


# ═══════════════════════════════════════════
# 5. Reduce Phase：重建 L1 索引
# ═══════════════════════════════════════════

def reduce_rebuild_index(topic_metas: list[dict]) -> str:
    """
    对所有 Topic 的元数据执行 Reduce，重建 L1_INDEX.md。
    如果 LLM 不可用，使用规则引擎自动重建。
    """
    # 尝试用 LLM 重建（更智能的摘要）
    prompt_path = PROMPTS_DIR / "REDUCE_INDEX.md"
    if prompt_path.exists() and LLM_API_KEY:
        system_prompt = prompt_path.read_text(encoding="utf-8")
        user_prompt = "以下是所有 Topic 的元数据：\n" + json.dumps(
            topic_metas, ensure_ascii=False, indent=2
        )
        result = call_llm(system_prompt, user_prompt)
        if result:
            return result

    # ── Fallback：规则引擎自动重建 ──
    now = datetime.datetime.now().isoformat(timespec="minutes")
    lines = [
        "# L1 Memory Index\n",
        f"> 自动生成，请勿手动编辑。上次重建：{now}\n",
    ]

    # 分类
    permanent = [t for t in topic_metas if t.get("is_permanent")]
    active = [t for t in topic_metas if not t.get("is_permanent")]
    active.sort(key=lambda x: x.get("last_updated", ""), reverse=True)

    if permanent:
        lines.append("\n## Permanent (目标与梦想)")
        for t in permanent:
            pointer = f"- [目标] {t['summary'][:100]} → 见 {t['file']}"
            lines.append(pointer[:L1_MAX_LINE_LENGTH])

    if active:
        lines.append("\n## Active (近期活跃)")
        for t in active[:L1_MAX_LINES - len(lines) - 5]:
            tag = t.get("tag", "主题")
            pointer = f"- [{tag}] {t['summary'][:100]} → 见 {t['file']}"
            lines.append(pointer[:L1_MAX_LINE_LENGTH])

    lines.append("\n---")
    lines.append(f"*索引行数：{len(lines)}/{L1_MAX_LINES}*")
    lines.append(f"*Topic 文件总数：{len(topic_metas)}*")
    lines.append(f"*最后一次 Dream 整理时间：{now}*")

    return "\n".join(lines)


# ═══════════════════════════════════════════
# 6. 老化检测与归档
# ═══════════════════════════════════════════

def detect_aging() -> tuple[list[str], list[str]]:
    """
    检测老化 Topic。
    返回 (warned_topics, archive_candidates)
    """
    warned = []
    candidates = []
    now = datetime.datetime.now()

    for topic in l2_list_topics():
        try:
            last_mod = datetime.datetime.fromisoformat(topic["last_modified"])
        except (ValueError, KeyError):
            continue

        days_old = (now - last_mod).days

        # 检查是否为 permanent（通过文件内容）
        content = l2_load_topic(TOPICS_DIR / f"{topic['name']}.md")
        if "[permanent]" in content.lower():
            continue  # 永久豁免

        if days_old >= AGING_ARCHIVE_DAYS:
            candidates.append(topic["name"])
        elif days_old >= AGING_WARN_DAYS:
            warned.append(topic["name"])

    return warned, candidates


def archive_topics(topic_names: list[str]):
    """将 Topic 文件移至 archive/ 目录。"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d")
    for name in topic_names:
        src = TOPICS_DIR / f"{name}.md"
        if src.exists():
            dst = ARCHIVE_DIR / f"{name}_{today}.md"
            shutil.move(str(src), str(dst))
            print(f"[GC] 归档: {src.name} → {dst.name}")


# ═══════════════════════════════════════════
# 7. 备份（带清理策略）
# ═══════════════════════════════════════════

def create_backup():
    """创建 workspace 备份，并清理旧备份保留最近 N 个。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    from config import KERNEL_ROOT
    backup_name = BACKUP_DIR / f"backup-{today}"
    shutil.make_archive(str(backup_name), "zip", str(KERNEL_ROOT / "memory"))
    print(f"[GC] 备份完成: {backup_name}.zip")

    # 清理旧备份
    backups = sorted(BACKUP_DIR.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime)
    while len(backups) > BACKUP_RETENTION_COUNT:
        old = backups.pop(0)
        old.unlink()
        print(f"[GC] 清理旧备份: {old.name}")


# ═══════════════════════════════════════════
# 8. 主流程：Dream 五阶段执行
# ═══════════════════════════════════════════

def run_dream(force: bool = False):
    """
    执行 AutoDream 五阶段整理。
    force=True 时跳过三重门检查（相当于用户手动 /dream）。
    """
    # ── 前置检查 ──
    if not force:
        ok, reason = should_dream()
        if not ok:
            print(f"[Dream] 未触发: {reason}")
            return

    print("=" * 60)
    print("[Dream] 🌙 开始记忆整理...")
    print("=" * 60)

    acquire_lock("phase_1_orient")

    try:
        # ════ 阶段 1：定向 (Orient) ════
        print("\n[阶段 1/5] 定向：扫描记忆系统健康度...")
        index_text = l1_load_index()
        index_lines = len(index_text.splitlines())
        topics = l2_list_topics()
        archive_count = len(list(ARCHIVE_DIR.glob("*.md"))) if ARCHIVE_DIR.exists() else 0
        inbox_entries = wal_read_all()

        oversized = [t for t in topics if t["est_tokens"] > TOPIC_MAX_TOKENS]

        print(f"  索引: {index_lines}/{L1_MAX_LINES} 行")
        print(f"  Topic 文件: {len(topics)} 个")
        print(f"  归档文件: {archive_count} 个")
        print(f"  Inbox 积压: {len(inbox_entries)} 条")
        print(f"  超大 Topic: {len(oversized)} 个")
        health = "良好" if (index_lines < L1_MAX_LINES * 0.8 and not oversized) else "需要整理"
        print(f"  健康度: {health}")

        # ════ 阶段 2：收集 (Gather) ════
        update_lock_stage("phase_2_gather")
        print(f"\n[阶段 2/5] 收集：处理 WAL Inbox...")

        if not inbox_entries:
            print("  Inbox 为空，跳过收集与整合阶段")
        else:
            # 按 topic 分组
            grouped = defaultdict(list)
            for entry in inbox_entries:
                grouped[entry.get("topic", "uncategorized")].append(entry)
            print(f"  提取 {len(inbox_entries)} 条记录，涉及 {len(grouped)} 个主题")

            # ════ 阶段 3：整合 (Consolidate) — Map Phase ════
            update_lock_stage("phase_3_consolidate")
            print(f"\n[阶段 3/5] 整合：对每个 Topic 执行独立微压缩...")

            conflicts_resolved = 0
            compressed_count = 0

            for topic_name, entries in grouped.items():
                topic_path = TOPICS_DIR / f"{topic_name}.md"

                # 读取现有内容（如果没有则创建空文件）
                if topic_path.exists():
                    existing = topic_path.read_text(encoding="utf-8")
                else:
                    # 从模板创建新 Topic
                    template_path = PROMPTS_DIR / "TOPIC_TEMPLATE.md"
                    existing = template_path.read_text(encoding="utf-8").replace(
                        "{topic_name}", topic_name
                    ).replace("{created_at}", datetime.datetime.now().isoformat())
                    topic_path.parent.mkdir(parents=True, exist_ok=True)

                # Map：调用 LLM 执行合并
                new_content = map_compact(topic_name, existing, entries)
                topic_path.write_text(new_content, encoding="utf-8")

                # 统计
                if l2_estimate_tokens(existing) > TOPIC_MAX_TOKENS:
                    compressed_count += 1
                conflicts_resolved += len(entries)

                print(f"  ✓ {topic_name}: 合并 {len(entries)} 条记录")

            print(f"  整合完成。处理 {len(grouped)} 个 Topic，合并 {conflicts_resolved} 条记录")

        # ════ 阶段 4：老化检测 (Aging Detection) ════
        update_lock_stage("phase_4_aging")
        print(f"\n[阶段 4/5] 老化检测...")
        warned, archive_candidates = detect_aging()
        print(f"  老化警告: {len(warned)} 个主题")
        print(f"  归档候选: {len(archive_candidates)} 个主题")

        if archive_candidates:
            archive_topics(archive_candidates)

        # ════ 阶段 5：剪枝与重建 (Prune & Rebuild) ════
        update_lock_stage("phase_5_rebuild")
        print(f"\n[阶段 5/5] 剪枝与重建 L1 索引...")

        # 收集所有 Topic 的元数据
        topic_metas = []
        for t in l2_list_topics():
            content = l2_load_topic(TOPICS_DIR / f"{t['name']}.md")
            first_line = content.split("\n")[0] if content else t["name"]
            summary = first_line.replace("#", "").strip()[:100]
            topic_metas.append({
                "topic": t["name"],
                "file": t["file"],
                "summary": summary,
                "token_count": t["est_tokens"],
                "last_updated": t["last_modified"],
                "is_permanent": "[permanent]" in content.lower(),
                "tag": "目标" if "[permanent]" in content.lower() else "主题",
            })

        # Reduce：重建 L1 索引
        new_index = reduce_rebuild_index(topic_metas)
        L1_INDEX_PATH.write_text(new_index, encoding="utf-8")

        # Commit：清空 Inbox
        wal_clear()
        session_reset()

        new_lines = len(new_index.splitlines())
        print(f"  索引重建完成: {new_lines}/{L1_MAX_LINES} 行")
        print(f"  WAL Inbox 已清空")
        print(f"  会话计数器已重置")

        # 备份
        create_backup()

        next_dream = (
            datetime.datetime.now() +
            datetime.timedelta(hours=DREAM_TIME_GATE_HOURS)
        ).strftime("%Y-%m-%d %H:%M")

        print("\n" + "=" * 60)
        print(f"[Dream] ✅ 整理完成！")
        print(f"  归档: {len(archive_candidates)} 个过期主题")
        print(f"  索引: {new_lines} 行")
        print(f"  下次建议整理时间: {next_dream}")
        print("=" * 60)

    except Exception as e:
        print(f"\n[Dream] ❌ 整理异常中断: {e}")
        raise
    finally:
        release_lock()


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]

    if "--force" in args or "/dream" in args:
        run_dream(force=True)
    elif "--check" in args:
        ok, reason = should_dream()
        print(f"三重门状态: {'全开' if ok else '未满足'}")
        print(f"原因: {reason}")
    elif "--backup" in args:
        create_backup()
    else:
        print("用法:")
        print("  python gc_worker.py --force    # 强制执行 Dream 整理")
        print("  python gc_worker.py --check    # 检查三重门状态")
        print("  python gc_worker.py --backup   # 手动备份")
        print("\n自动触发模式下，三重门同时满足时自动执行。")
        run_dream(force=False)
