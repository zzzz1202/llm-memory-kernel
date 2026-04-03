# 任务：全局索引重建 (Reduce Phase)

> 你是索引重建引擎。基于所有 Topic 文件的元数据摘要，重新生成 L1_INDEX.md。

## 输入

一组 Topic 文件的元数据，每条格式为：

```jsonl
{"topic": "topic_name", "file": "topics/topic_name.md", "summary": "一句话核心摘要", "token_count": 1200, "last_updated": "2026-04-03", "is_permanent": false}
```

## 执行步骤

1. **排序**：按以下优先级排序
   - `is_permanent: true` 的条目（目标/梦想）永远置顶
   - 其次按 `last_updated` 降序（最近更新的在前）
2. **生成指针行**：每个 Topic 生成一行索引指针
   - 格式：`- [{tag}] {summary} → 见 topics/{topic_name}.md`
   - 每行不超过 150 字符，超出则截断摘要
3. **严格控制行数**：总行数 ≤ 200 行（含标题和元信息）
4. **溢出策略**：如果 Topic 总数超出 200 行容量
   - 优先保留 `is_permanent` 条目
   - 其次保留最近 30 天内更新过的条目
   - 其余条目降级：从 L1 索引中移除指针（但不删除 Topic 文件本身）
   - 在索引底部添加 `*注意：有 N 个低频主题未在索引中列出，可通过语义检索访问*`

## 输出格式

```markdown
# L1 Memory Index

> 自动生成，请勿手动编辑。上次重建：{current_datetime}

## Permanent (目标与梦想)
- [目标] {summary} → 见 topics/{file}

## Active (近期活跃)
- [{tag}] {summary} → 见 topics/{file}

## Reference (低频参考)
- [{tag}] {summary} → 见 topics/{file}

---
*索引行数：{line_count}/200*
*Topic 文件总数：{total_topics}*
*最后一次 Dream 整理时间：{dream_time}*
```

## 约束

- 绝不删除任何 Topic 文件，只控制索引中是否出现指针
- 输出必须是可直接写入文件的完整 Markdown
