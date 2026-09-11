# 本地创作数据

官方启动脚本默认把 SQLite 数据库、会话历史和项目资产保存在 `backend/data`，每部小说的数据库位于 `backend/data/projects/<项目ID>/project.db`。仅当 `NOVEL_DATA_DIR` 显式指向此目录时，实际数据才保存在这里。

实际数据目录已被 Git 忽略。备份时请复制当前使用的完整数据目录，或在界面中点击“导出项目备份”；重新启动时应继续使用同一个 `NOVEL_DATA_DIR`。

测试使用系统临时目录，不会写入这里。
