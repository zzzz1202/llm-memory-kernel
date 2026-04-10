# Raw Sources（原始素材）

> 将文章、笔记、PDF 转换后的 Markdown 等原始素材放在此目录下。
> AI 会通过 ingest 流程读取并提取关键信息，整合进 Wiki 式知识库。

## 使用方式

1. 将文件放入本目录（支持 `.md`、`.txt`）
2. 运行 `python scripts/ingest.py raw/你的文件.md`
3. AI 自动提取关键信息，写入 WAL，等待 Dream 整合

## 注意事项

- 原始文件是**不可变的**（immutable），AI 只读不写
- 处理完的文件不会被删除，始终保留作为溯源依据
- 支持批量处理：`python scripts/ingest.py raw/*.md`
