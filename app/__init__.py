"""StoryForge —— AI 短剧生成工作台（本地 Web 应用）。

对标腾讯开源项目 ``Tencent/digitalhuman`` 的 ScriptAgent 框架：
粗糙对话 →（ScriptAgent）→ 分镜脚本 →（DirectorAgent）→ 多镜头视频
→（FFmpeg 拼接）→ 成片 →（CriticAgent）→ 评估报告。
"""

from __future__ import annotations

__all__ = ["__version__", "__app_name__", "__description__"]

__version__: str = "1.0.0"
__app_name__: str = "StoryForge"
__description__: str = "AI 短剧生成工作台：对话 → 脚本 → 多镜头视频 → 评估报告"
