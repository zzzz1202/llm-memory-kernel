"""
ingest.py — 原始素材摄入工具
纯数据层：将 raw/ 目录下的文件内容写入 WAL，供 Agent 后续处理。
不调用任何外部 LLM，智能提取由调用方 Agent 自行完成。
"""
import sys
import datetime
from pathlib import Path

# 确保能 import 同目录的模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import KERNEL_ROOT
from memory_router import wal_append


LOG_PATH = KERNEL_ROOT / "memory" / "log.md"
RAW_DIR = KERNEL_ROOT / "raw"


def append_log(action: str, description: str):
    """追加一条人类可读的日志到 log.md。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{now}] {action} | {description}\n"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if LOG_PATH.exists():
        content = LOG_PATH.read_text(encoding="utf-8")
        if "\n---\n" in content:
            header, body = content.split("\n---\n", 1)
            content = header + "\n---\n" + entry + body
        else:
            content += entry
    else:
        content = "# 操作日志 (Chronological Log)\n\n---\n" + entry
    LOG_PATH.write_text(content, encoding="utf-8")


def ingest_file(file_path: Path) -> int:
    """
    将单个素材文件的内容写入 WAL。
    取前 500 字作为摘要，整篇保留溯源。
    返回写入的 WAL 条目数。
    """
    text = file_path.read_text(encoding="utf-8")
    source_name = file_path.name
    print(f"[Ingest] 处理文件: {source_name} ({len(text)} 字符)")

    # 生成 topic 名：以文件名为基础
    topic = file_path.stem.replace(" ", "_").replace("-", "_").lower()

    # 取前 500 字作为 WAL 内容
    summary = text[:500].replace("\n", " ").strip()
    if len(text) > 500:
        summary += "..."

    wal_append(
        topic=f"source_{topic}",
        content=summary,
        action="add_fact",
        source=f"raw/{source_name}",
    )
    print(f"  → 写入 1 条 WAL 记录 (topic: source_{topic})")
    return 1


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python ingest.py <file>        # 处理单个文件")
        print("  python ingest.py raw/*.md       # 批量处理")
        print("  python ingest.py --list         # 列出 raw/ 下的文件")
        print()
        print("说明：本工具只负责将素材存入 WAL。")
        print("智能提取、分类、压缩由调用方 Agent 自行完成。")
        return

    if sys.argv[1] == "--list":
        if not RAW_DIR.exists():
            print("raw/ 目录不存在")
            return
        files = [f for f in RAW_DIR.glob("*") if f.is_file() and f.name != "README.md"]
        if not files:
            print("raw/ 目录为空（不含 README.md）")
        else:
            for f in files:
                print(f"  {f.name} ({f.stat().st_size} bytes)")
        return

    total_entries = 0
    total_files = 0

    for arg in sys.argv[1:]:
        file_path = Path(arg)
        if not file_path.exists():
            file_path = RAW_DIR / arg
        if not file_path.exists():
            print(f"[Ingest] 文件不存在: {arg}")
            continue
        if file_path.is_dir():
            continue

        total_entries += ingest_file(file_path)
        total_files += 1

    if total_files > 0:
        append_log("ingest", f"处理 {total_files} 个文件，写入 {total_entries} 条 WAL 记录")

    print(f"\n[Ingest] 完成。共处理 {total_files} 个文件，写入 {total_entries} 条记录。")
    print("提示：运行 `python gc_worker.py --force` 将 WAL 记录整合进 Topic 文件。")


if __name__ == "__main__":
    main()
