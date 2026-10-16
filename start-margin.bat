@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

echo.
echo   Margin · 研发辅助 Agent 工作台
echo   ---------------------------------------------
echo   启动后请用浏览器打开： http://127.0.0.1:8000/
echo   不要双击 index.html —— 那样没有后端，回形针等
echo   需要服务端的功能（浏览电脑目录、真实运行）都用不了。
echo.
echo   关闭这个窗口 = 停止服务。用完再关。
echo   ---------------------------------------------
echo.

python -m harness.server --port 8000
set CODE=%ERRORLEVEL%

if not "%CODE%"=="0" (
  echo.
  echo   [启动失败，退出码 %CODE%]
  echo   常见原因：
  echo     1) 没有装 Python，或 python 不在 PATH 里
  echo     2) 8000 端口已被占用（上次的服务还开着？）
  echo   手动启动：在该目录执行  python -m harness.server --port 8000
  echo.
  pause
)
