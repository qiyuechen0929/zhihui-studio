#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智绘工坊 · 桌面版入口

用 pywebview（WebView2）把本地网页嵌进原生窗口，全程不跳转浏览器：
双击 exe（或 python app.py）→ 弹出应用窗口 → 关闭窗口即退出。

启动说明：
    python app.py           # 桌面窗口模式
    python server.py        # 原浏览器模式（保留）

打包：pyinstaller 时 server.py 的静态资源、lib/ 会被打进 exe。
"""

import os
import sys
import traceback
# 打包（PyInstaller）时：把临时解压目录 _MEIPASS 加进模块搜索路径，确保
# 能 import 到内置的 server 模块。
if getattr(sys, "frozen", False):
    _MEIPASS = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if _MEIPASS not in sys.path:
        sys.path.insert(0, _MEIPASS)

# GUI 打包后无控制台，把错误写进 exe 旁的 app.log 便于排查
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)), "app.log")


def _log(msg):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _create_shortcuts():
    """首次运行创建桌面快捷方式（可选，失败不影响主程序）。

    用 Windows Script Host 的 WScript.Shell 创建 .lnk，纯标准库实现。
    只创建一次：快捷方式已存在则跳过（不会重复创建/覆盖用户自定义）。
    """
    import subprocess

    if not getattr(sys, "frozen", False):
        return  # 源码运行（python app.py）不自动建快捷方式

    exe_path = os.path.abspath(sys.executable)
    exe_name = os.path.splitext(os.path.basename(exe_path))[0]
    shortcut_name = exe_name + ".lnk"  # 如 智绘工坊.lnk

    desktop = None
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            desktop = winreg.QueryValueEx(key, "Desktop")[0]
    except Exception:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")

    if not desktop or not os.path.isdir(desktop):
        return

    shortcut_path = os.path.join(desktop, shortcut_name)
    if os.path.exists(shortcut_path):
        return  # 已存在，不覆盖

    # 用 WSH 创建快捷方式（免第三方库）
    ps = f'''$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut('{shortcut_path}')
$sc.TargetPath = '{exe_path}'
$sc.WorkingDirectory = '{os.path.dirname(exe_path)}'
$sc.Description = '智绘工坊 - AI 思维导图与图表'
$sc.Save()'''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and os.path.exists(shortcut_path):
            _log(f"[app] 已在桌面创建快捷方式: {shortcut_path}")
        else:
            _log(f"[app] 桌面快捷方式创建失败: {result.stderr.decode('utf-8', 'ignore')[:100]}")
    except Exception as e:
        _log(f"[app] 桌面快捷方式创建异常: {e}")


import server as mindmap_server  # noqa: E402


def main():
    try:
        _create_shortcuts()  # 首次运行创建桌面快捷方式
        # 启动本地 HTTP 服务（后台线程），拿到实际端口
        server, port = mindmap_server.start_server(port=8000, host="127.0.0.1", open_browser=False)
        _log(f"[app] HTTP 服务已启动，端口 {port}")

        import webview

        # 启用文件下载：前端「导出图片/Word/PDF/下载配置」都走 blob + a[download]，
        # pywebview 默认 ALLOW_DOWNLOADS=False 会取消下载，这里必须打开。
        # 启用后点击导出会弹出系统「另存为」对话框，用户自己选保存位置。
        try:
            webview.settings['ALLOW_DOWNLOADS'] = True
        except Exception:
            pass

        # 打开原生窗口：加载本地网页，标题、窗口尺寸、居中
        window = webview.create_window(
            "智绘工坊 — AI 思维导图 · 图表（作者：陈启粤）",
            f"http://127.0.0.1:{port}/",
            width=1180,
            height=800,
            min_size=(900, 620),
            resizable=True,
            background_color="#f5f7fa",
        )

        # 窗口关闭时，自动停止 HTTP 服务线程
        window.events.closed += lambda: _shutdown(server)

        webview.start(debug=False)
        # webview.start() 返回后窗口已关闭，清理服务
        _shutdown(server)
        _log("[app] 正常退出")
    except Exception:
        _log("[app] 启动失败:\n" + traceback.format_exc())
        raise


def _shutdown(server):
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
