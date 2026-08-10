# 智绘工坊 - PyInstaller 打包配置
# 用法：
#   pyinstaller build.spec
# 说明：
#   - app.py 为桌面入口（pywebview 窗口，不跳浏览器）
#   - 静态资源 index.html、lib/（含 markmap/ECharts/Mermaid/KaTeX 全套离线库）
#     打进 exe 的 _MEIPASS 临时目录
#   - config.json / history.json / .env 均为用户隐私数据，不进包
#   - pywebview 自带官方 PyInstaller hook，自动收集其 js/lib 运行时文件

import os

project_dir = os.getcwd()

a = Analysis(
    ["app.py"],
    pathex=[project_dir],
    binaries=[],
    datas=[
        ("index.html", "."),                    # 前端主页面
        ("lib", "lib"),                          # 本地渲染库（离线可用）
        ("server.py", "."),                      # 后端模块（供 app.py import）
    ],
    hiddenimports=[
        "webview.platforms.edgechromium",        # WebView2（Win10/11 自带）
        "webview.platforms.winforms",            # WinForms 窗口宿主
        "webview.platforms.win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5", "PyQt6", "PySide2", "PySide6",  # 未用，避免误打包
        "matplotlib", "numpy", "PIL",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="智绘工坊",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 纯 GUI：不弹黑窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
