@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 断线时自动重试的次数：pip 内部重试 + 整个安装步骤重新执行。
set "PIP_RETRIES=10"
set "PIP_TIMEOUT=30"
set "INSTALL_ATTEMPTS=3"
set "MAX_REBUILDS=3"
set "rebuilds=0"

if exist ".venv\Scripts\python.exe" goto dependencies

echo 正在查找 Python 3.10 或更新版本...
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_with_py
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_with_python

echo.
echo 没有找到可用的 Python 3.10 或更新版本。
echo 请先安装 Windows 版 Python，并勾选 Add Python to PATH，然后再次双击此文件。
echo 下载地址：https://www.python.org/downloads/windows/
pause
exit /b 1

:create_with_py
echo 正在本目录创建独立的 Python 环境...
py -3 -m venv ".venv"
if errorlevel 1 goto failed
goto dependencies

:create_with_python
echo 正在本目录创建独立的 Python 环境...
python -m venv ".venv"
if errorlevel 1 goto failed

:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto ready
rem 虚拟环境里的解释器指向创建它的那台电脑（pyvenv.cfg 记录绝对路径）。
rem 换电脑或换 Python 位置后这里必然失败，此时直接重建，不必手动删目录。
set /a rebuilds+=1
if %rebuilds% gtr %MAX_REBUILDS% goto failed
echo 检测到现有的 .venv 不可用（通常是复制自其他电脑），正在重建...
rmdir /s /q ".venv"
if exist ".venv\Scripts\python.exe" goto failed
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_with_py
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_with_python
echo.
echo 没有找到可用的 Python 3.10 或更新版本，无法重建环境。
echo 请先安装 Windows 版 Python，并勾选 Add Python to PATH，然后再次双击此文件。
echo 下载地址：https://www.python.org/downloads/windows/
pause
exit /b 1

:ready
".venv\Scripts\python.exe" -c "import tkinter; import customtkinter; from PIL import Image, ImageOps; import send2trash" >nul 2>&1
if not errorlevel 1 goto launch
if not exist ".venv\Scripts\python.exe" goto failed
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if not errorlevel 1 goto install_dependencies
echo 正在修复未完成的 pip 安装...
".venv\Scripts\python.exe" -m ensurepip --upgrade --default-pip
if errorlevel 1 goto failed

:install_dependencies
echo 首次运行需要安装 CustomTkinter、Pillow 和 Send2Trash，请保持网络连接...
echo 网络中断会自动重试，最多 %INSTALL_ATTEMPTS% 轮。
set "attempt=0"
:install_attempt
set /a attempt+=1
".venv\Scripts\python.exe" -m pip install --retries %PIP_RETRIES% --timeout %PIP_TIMEOUT% -r "%~dp0requirements.txt"
if not errorlevel 1 goto verify_dependencies
if %attempt% geq %INSTALL_ATTEMPTS% goto failed
echo.
echo 第 %attempt% 轮安装未完成，5 秒后重试...
timeout /t 5 /nobreak >nul
goto install_attempt

:verify_dependencies
".venv\Scripts\python.exe" -c "import tkinter; import customtkinter; from PIL import Image, ImageOps; import send2trash" >nul 2>&1
if errorlevel 1 goto failed

:launch
if /i "%~1"=="--check" goto check_environment
echo 正在打开 QQ 表情包工具...
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0qq_emoji_tool.py"
exit /b 0

:check_environment
".venv\Scripts\python.exe" -c "import sys, tkinter; from importlib.metadata import version; print('Python', sys.version.split()[0]); print('Tk', tkinter.TkVersion); print('CustomTkinter', version('customtkinter')); print('Pillow', version('Pillow')); print('Send2Trash', version('Send2Trash'))"
exit /b %errorlevel%

:failed
echo.
echo 环境准备失败。请查看上面的错误信息。
echo 如果缺少 tkinter，请使用包含 Tcl/Tk 的 Windows 版 Python 安装程序。
echo 也可以按 README.md 的说明手动安装依赖并运行。
pause
exit /b 1
