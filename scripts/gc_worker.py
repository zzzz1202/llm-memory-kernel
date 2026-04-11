"""
gc_worker.py — 异步垃圾回收器 (Map-Reduce GC)
核心职责（纯数据层，不依赖外部 LLM）：
  1. 检查三重门触发条件
  2. Map Phase：按 Topic 分组处理 WAL Inbox，规则引擎追加合并
  3. Reduce Phase：规则引擎重建 L1 索引
  4. Lint：健康检查（死链、孤儿页、信息空洞、交叉引用）
  5. Commit：清空 Inbox，归档老化数据，释放锁
  6. 追加 log.md 记录
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
# 3. Map Phase：规则引擎追加合并（纯数据层，不依赖外部 LLM）
# ═══════════════════════════════════════════

def map_compact(topic_name: str, existing_content: str, new_entries: list[dict]) -> str:
    """
    对单个 Topic 执行 Map 阶段的追加合并。
    纯规则引擎：将新日志按时间顺序追加到 Topic 文件末尾。
    智能压缩/去重/矛盾解决由调用方 Agent（如 Antigravity、OpenClaw）自行处理。
    """
    entries_text = "\n".join(
        f"- [{e.get('timestamp', '?')}] {e.get('content', '')}" for e in new_entries
    )
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return existing_content + f"\n\n## 更新 ({now})\n" + entries_text


# ═══════════════════════════════════════════
# 4. Reduce Phase：规则引擎重建 L1 索引
# ═══════════════════════════════════════════

def reduce_rebuild_index(topic_metas: list[dict]) -> str:
    """
    对所有 Topic 的元数据执行 Reduce，重建 L1_INDEX.md。
    纯规则引擎，不依赖外部 LLM。
    """
    now = datetime.datetime.now().isoformat(timespec="minutes")
    lines = [
        "# L1 Memory Index\n",
        f"> 自动生成，请勿手动编辑。上次重建：{now}\n",
    ]

    # ═══ Critical Facts（核心事实速览，≤10 行）═══
    # Agent 启动时只需读这个区域即可获得用户身份和最关键的上下文。
    # 借鉴 MemPalace L0+L1 分层加载策略：170 tokens 秒读核心信息。
    lines.append("## Critical Facts（核心速览）")
    lines.append("> Agent 启动时优先读此区域。如需详情再按需加载 Topic 文件。\n")

    critical_lines = []
    for t in topic_metas:
        topic_path = TOPICS_DIR / f"{t['topic']}.md"
        if not topic_path.exists():
            continue
        content = topic_path.read_text(encoding="utf-8")
        # 提取 ## 核心事实 下的第一批条目（非 [待填充]）
        in_core = False
        for line in content.splitlines():
            if "核心事实" in line or "Critical" in line:
                in_core = True
                continue
            if in_core:
                if line.startswith("##"):  # 遇到下一个标题，停止
                    break
                stripped = line.strip()
                if stripped.startswith("- ") and "[待填充]" not in stripped:
                    fact = stripped[2:].strip()[:100]
                    critical_lines.append(f"- **{t['topic']}**: {fact}")

    if critical_lines:
        for cl in critical_lines[:10]:  # 最多 10 条
            lines.append(cl)
    else:
        lines.append("- （尚无核心事实，请通过对话积累）")

    # ═══ 分类索引 ═══
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
# 7. Lint 健康检查（融合 Karpathy Wiki Lint 理念）
# ═══════════════════════════════════════════

def lint_check() -> dict:
    """
    Wiki 式健康检查。检测：
    - 死链：L1 索引指向不存在的 Topic 文件
    - 孤儿页：Topic 文件未被 L1 索引引用
    - 信息空洞：包含 [待填充] 的条目
    - 缺失交叉引用：内容相关但未互相引用的 Topic 对
    """
    import re
    errors = []   # 必须修复
    warnings = [] # 建议修复
    suggestions = [] # 可选优化

    index_text = l1_load_index()
    topics_on_disk = {p.stem for p in TOPICS_DIR.glob("*.md")} if TOPICS_DIR.exists() else set()

    # 提取索引中引用的 topic 文件名
    pointer_pattern = re.compile(r"topics/([\w\-]+)\.md", re.UNICODE)
    topics_in_index = set(pointer_pattern.findall(index_text))

    # 死链检测
    for name in topics_in_index:
        if name not in topics_on_disk:
            errors.append(f"[死链] L1_INDEX 引用了 topics/{name}.md，但文件不存在")

    # 孤儿页检测
    for name in topics_on_disk:
        if name not in topics_in_index:
            warnings.append(f"[孤儿] topics/{name}.md 未在 L1_INDEX 中引用")

    # 信息空洞检测
    topic_contents = {}
    for name in topics_on_disk:
        content = l2_load_topic(TOPICS_DIR / f"{name}.md")
        topic_contents[name] = content
        if "[待填充]" in content or "(暂无)" in content:
            suggestions.append(f"[空洞] topics/{name}.md 仍有未填充内容")

    # 交叉引用检测：如果一个 Topic 的名字出现在另一个 Topic 的内容中，但没有显式引用
    ref_pattern = re.compile(r"topics/([\w\-]+)\.md", re.UNICODE)
    for name_a in topics_on_disk:
        content_a = topic_contents.get(name_a, "")
        refs_in_a = set(ref_pattern.findall(content_a))
        for name_b in topics_on_disk:
            if name_a == name_b:
                continue
            # 检查 name_b 是否在 name_a 的内容中被提及（以关键词形式）
            # 将 snake_case 转为可能出现的中文关键词
            b_readable = name_b.replace("_", " ").replace("-", " ")
            if (b_readable in content_a.lower() or name_b in content_a.lower()) and name_b not in refs_in_a:
                suggestions.append(
                    f"[交叉引用] topics/{name_a}.md 提及了 \"{name_b}\"，建议添加 → 另见 topics/{name_b}.md"
                )

    return {
        "errors": errors,
        "warnings": warnings,
        "suggestions": suggestions,
    }


# ═══════════════════════════════════════════
# 8. log.md 日志追加
# ═══════════════════════════════════════════

def append_log(action: str, description: str):
    """追加人类可读的日志到 memory/log.md。"""
    from config import KERNEL_ROOT
    log_path = KERNEL_ROOT / "memory" / "log.md"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{now}] {action} | {description}\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if log_path.exists():
        content = log_path.read_text(encoding="utf-8")
        if "\n---\n" in content:
            header, body = content.split("\n---\n", 1)
            content = header + "\n---\n" + entry + body
        else:
            content += entry
    else:
        content = "# 操作日志 (Chronological Log)\n\n---\n" + entry
    log_path.write_text(content, encoding="utf-8")


# ═══════════════════════════════════════════
# 9. 备份（带清理策略）
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

def run_dream(force: bool = False, dry_run: bool = False):
    """
    执行 AutoDream 六阶段整理。
    force=True 时跳过三重门检查。
    dry_run=True 时只展示方案不执行（范凯式确认机制）。
    """
    # ── 前置检查 ──
    if not force:
        ok, reason = should_dream()
        if not ok:
            print(f"[Dream] 未触发: {reason}")
            return

    if dry_run:
        print("=" * 60)
        print("[Dream] 🔍 预览模式（不会执行任何修改）")
        print("=" * 60)
    else:
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

                # Map：合并新内容
                new_content = map_compact(topic_name, existing, entries)
                if dry_run:
                    print(f"  [预览] {topic_name}: 将合并 {len(entries)} 条记录")
                    for e in entries:
                        print(f"    + {e.get('content', '')[:80]}")
                else:
                    topic_path.write_text(new_content, encoding="utf-8")

                # 统计
                if l2_estimate_tokens(existing) > TOPIC_MAX_TOKENS:
                    compressed_count += 1
                conflicts_resolved += len(entries)

                print(f"  ✓ {topic_name}: 合并 {len(entries)} 条记录")

            print(f"  整合完成。处理 {len(grouped)} 个 Topic，合并 {conflicts_resolved} 条记录")

        # ════ 阶段 4：老化检测 (Aging Detection) ════
        update_lock_stage("phase_4_aging")
        print(f"\n[阶段 4/6] 老化检测...")
        warned, archive_candidates = detect_aging()
        print(f"  老化警告: {len(warned)} 个主题")
        print(f"  归档候选: {len(archive_candidates)} 个主题")

        if archive_candidates:
            if dry_run:
                for name in archive_candidates:
                    print(f"  [预览] 将归档: topics/{name}.md")
            else:
                archive_topics(archive_candidates)

        # ════ 阶段 5：Lint 健康检查 ════
        update_lock_stage("phase_5_lint")
        print(f"\n[阶段 5/6] Lint 健康检查...")
        lint_result = lint_check()
        lint_errors = len(lint_result["errors"])
        lint_warnings = len(lint_result["warnings"])
        lint_suggestions = len(lint_result["suggestions"])
        for e in lint_result["errors"]:
            print(f"  🔴 {e}")
        for w in lint_result["warnings"]:
            print(f"  🟡 {w}")
        for s in lint_result["suggestions"]:
            print(f"  🟢 {s}")
        print(f"  健康度: {lint_errors} 错误, {lint_warnings} 警告, {lint_suggestions} 建议")

        # ════ 阶段 6：剪枝与重建 (Prune & Rebuild) ════
        update_lock_stage("phase_6_rebuild")
        print(f"\n[阶段 6/6] 剪枝与重建 L1 索引...")

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

        if dry_run:
            print("\n" + "=" * 60)
            print("[Dream] 🔍 预览完成。以上是计划执行的操作。")
            print("确认后请运行: python gc_worker.py --force")
            print("=" * 60)
            return  # dry_run 不写文件、不清 WAL、不备份

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

        # 追加日志
        log_desc = (
            f"处理 {len(inbox_entries) if inbox_entries else 0} 条 WAL 记录, "
            f"归档 {len(archive_candidates)} 个主题, "
            f"索引 {new_lines} 行, "
            f"Lint: {lint_errors}E/{lint_warnings}W/{lint_suggestions}S"
        )
        append_log("dream", log_desc)

        print("\n" + "=" * 60)
        print(f"[Dream] ✅ 整理完成！")
        print(f"  归档: {len(archive_candidates)} 个过期主题")
        print(f"  索引: {new_lines} 行")
        print(f"  Lint: {lint_errors} 错误, {lint_warnings} 警告, {lint_suggestions} 建议")
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

    if "--dry-run" in args:
        run_dream(force=True, dry_run=True)
    elif "--force" in args or "/dream" in args:
        run_dream(force=True)
    elif "--check" in args:
        ok, reason = should_dream()
        print(f"三重门状态: {'全开' if ok else '未满足'}")
        print(f"原因: {reason}")
    elif "--backup" in args:
        create_backup()
    else:
        print("用法:")
        print("  python gc_worker.py --dry-run   # 🔍 预览方案（不执行，先看再决定）")
        print("  python gc_worker.py --force     # ✅ 确认执行 Dream 整理")
        print("  python gc_worker.py --check     # 检查三重门状态")
        print("  python gc_worker.py --backup    # 手动备份")
        print("\n推荐流程: --dry-run → 确认方案 → --force 执行")
