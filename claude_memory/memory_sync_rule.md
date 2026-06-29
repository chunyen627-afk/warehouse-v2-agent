---
name: memory_sync_rule
description: 改記憶要同步兩邊 — 全域 ~/.claude/ + 專案 warehouse_v2/claude_memory/（Git 用）
metadata:
  type: feedback
---

## 記憶同步規則（2026-06-29 確立）

專案有**兩個**記憶目錄，改動必須兩邊同時更新：

| 位置 | 用途 |
|------|------|
| `~/.claude/projects/C--Users-pjunm-.../memory/` | Claude Code 全域記憶（session 間持久） |
| `warehouse_v2/claude_memory/` | 專案本地副本（跟著 Git push/pull） |

**Why**：`claude_memory/` 隨 Git 上傳，讓其他協作者／RPI5 部署端也能看到專案記憶和踩雷記錄。全域記憶只有本機 Claude Code 看得到。

**How to apply**：
1. 改記憶時 → 先更新全域目錄（serena write_memory 或直接寫檔）
2. → 複製到 `warehouse_v2/claude_memory/`
3. → 更新兩邊的 `MEMORY.md` 索引
4. → `git add claude_memory/ && git commit`

關聯：[[warehouse_v2_project]]
