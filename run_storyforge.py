"""PyInstaller 冻结入口：启动 StoryForge 本地服务。

打包（onedir）后双击 ``StoryForge.exe`` 即执行本脚本：探测空闲端口、写入
``logs/port.txt``、自动打开浏览器并常驻运行；关闭窗口即停止服务。

非冻结时也可直接运行：``python run_storyforge.py``。
"""

from __future__ import annotations

from app.main import run

if __name__ == "__main__":
    run()
