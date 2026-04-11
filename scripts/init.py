"""
init.py — 记忆内核初始化工具
clone 仓库后运行一次，创建个人记忆空间。
用法：python scripts/init.py
"""
import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    KERNEL_ROOT, MEMORY_DIR, TOPICS_DIR, ARCHIVE_DIR,
    SYSTEM_DIR, WAL_INBOX_PATH, EMBEDDINGS_DIR,
    L1_INDEX_PATH, BACKUP_DIR,
)

TOPIC_TEMPLATE = KERNEL_ROOT / "core_prompts" / "TOPIC_TEMPLATE.md"
LOG_PATH = MEMORY_DIR / "log.md"


def init():
    print("=" * 50)
    print("🧠 LLM Memory Kernel — 初始化")
    print("=" * 50)

    # 创建目录结构
    dirs = [MEMORY_DIR, TOPICS_DIR, ARCHIVE_DIR, SYSTEM_DIR, EMBEDDINGS_DIR, BACKUP_DIR, KERNEL_ROOT / "raw"]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  ✅ {d.relative_to(KERNEL_ROOT)}/")

    # 创建 WAL（空文件）
    if not WAL_INBOX_PATH.exists():
        WAL_INBOX_PATH.write_text("", encoding="utf-8")
        print(f"  ✅ {WAL_INBOX_PATH.relative_to(KERNEL_ROOT)}")

    # 读取 Topic 模板
    if TOPIC_TEMPLATE.exists():
        template = TOPIC_TEMPLATE.read_text(encoding="utf-8")
    else:
        template = "# {title}\n\n> 创建于 {date}\n\n## 核心事实\n\n- [待填充]\n"

    # 创建默认 Topic 文件
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    default_topics = {
        "user_profile": {
            "title": "用户画像 (User Profile)",
            "tag": "[permanent]",
            "desc": "记录用户的身份、核心目标与背景信息。",
        },
        "preferences": {
            "title": "偏好设定 (Preferences)",
            "tag": "[permanent]",
            "desc": "记录用户的技术偏好、交互风格与习惯。",
        },
        "errors_and_lessons": {
            "title": "错误记录与自愈 (Errors & Lessons Learned)",
            "tag": "",
            "desc": "AI 犯过的错误以及修正记录，确保不重蹈覆辙。",
        },
    }

    for name, meta in default_topics.items():
        path = TOPICS_DIR / f"{name}.md"
        if not path.exists():
            content = f"# {meta['title']}\n\n"
            if meta['tag']:
                content += f"> 标记: {meta['tag']}\n"
            content += f"> 创建于 {now}\n\n"
            content += f"{meta['desc']}\n\n## 核心事实\n\n- [待填充]\n"
            path.write_text(content, encoding="utf-8")
            print(f"  ✅ topics/{name}.md")
        else:
            print(f"  ⏭️  topics/{name}.md（已存在，跳过）")

    # 创建初始 L1 索引
    if not L1_INDEX_PATH.exists():
        now_iso = datetime.datetime.now().isoformat(timespec="minutes")
        index = (
            "# L1 Memory Index\n\n"
            f"> 自动生成，请勿手动编辑。上次重建：{now_iso}\n\n"
            "## Critical Facts（核心速览）\n"
            "> Agent 启动时优先读此区域。如需详情再按需加载 Topic 文件。\n\n"
            "- （尚无核心事实，请通过对话积累）\n\n"
            "## Permanent (目标与梦想)\n"
            "- [目标] 主题：user_profile → 见 topics/user_profile.md\n"
            "- [目标] 主题：preferences → 见 topics/preferences.md\n\n"
            "## Active (近期活跃)\n"
            "- [主题] 错误记录与自愈 → 见 topics/errors_and_lessons.md\n\n"
            "---\n"
            f"*索引行数：14/200*\n"
            f"*Topic 文件总数：3*\n"
            f"*最后一次 Dream 整理时间：{now_iso}*\n"
        )
        L1_INDEX_PATH.write_text(index, encoding="utf-8")
        print(f"  ✅ L1_INDEX.md")
    else:
        print(f"  ⏭️  L1_INDEX.md（已存在，跳过）")

    # 创建初始日志
    if not LOG_PATH.exists():
        now_human = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        log = (
            "# 操作日志 (Chronological Log)\n\n"
            "> 按时间倒序记录所有记忆操作。人类可读，可用 grep 过滤。\n"
            "> 格式：`## [YYYY-MM-DD HH:MM] action | 描述`\n\n"
            "---\n\n"
            f"## [{now_human}] init | 记忆内核初始化\n"
            "- 创建核心 Topic：user_profile, preferences, errors_and_lessons\n"
            "- 生成初始 L1 索引\n"
        )
        LOG_PATH.write_text(log, encoding="utf-8")
        print(f"  ✅ log.md")

    print()
    print("🎉 初始化完成！")
    print()
    print("下一步：")
    print("  1. 将 SKILL.md 的内容注入你的 Agent 系统提示词中")
    print("  2. 让 Agent 调用 scripts/memory_router.py 的 wal_append() 写入记忆")
    print("  3. 定期运行 python scripts/gc_worker.py --force 整理记忆")
    print()


if __name__ == "__main__":
    init()
