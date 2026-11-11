@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem ===== 检查并补全 bin 目录下的依赖程序 =====
rem 缺失时通过 GitHub 加速镜像下载；可用 WARP_GH_MIRROR 覆盖（结尾必须带 /）。
call :ensure_deps
if errorlevel 1 (
    echo.
    echo [deps] 依赖下载失败，请检查网络后重试。
    echo        可通过环境变量指定其他加速地址，例如：
    echo        set WARP_GH_MIRROR=https://ghproxy.net/
    echo.
    if /i not "%WARP_DEPS_ONLY%"=="1" pause
    exit /b 1
)
if /i "%WARP_DEPS_ONLY%"=="1" exit /b 0

rem Find a working Python 3. Every candidate is verified by actually
rem running it. When nothing is found, a Python 3.12 installer is downloaded
rem and installed silently (per-user, no admin required), then detection reruns.
call :detect_python
if not defined PY (
    echo [python] No working Python 3 found, downloading Python 3.12 installer...
    call :install_python
    call :detect_python
)

rem Set AUTO_START_OPENCODE=1 to auto-launch start-opencode.bat after WARP is verified.
set "WC_ARGS=%*"
if /i "%AUTO_START_OPENCODE%"=="1" set "WC_ARGS=%WC_ARGS% --auto-start-opencode"

%PY% warp_control.py %WC_ARGS%
set "RC=%errorlevel%"
if not "%RC%"=="0" pause
exit /b %RC%

:nopython
echo.
echo [python] Auto-install failed. Please install Python 3.8 or newer from:
echo          https://www.python.org/downloads/
echo          During setup, enable "Add Python to PATH", then rerun this script.
echo.
pause
exit /b 1

rem ===== Python detection and auto-install subroutines =====

:detect_python
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    exit /b 0
)
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    exit /b 0
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
    if not defined PY (
        "%%~fD\python.exe" -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY="%%~fD\python.exe""
    )
)
if defined PY exit /b 0
for /d %%D in ("%ProgramFiles%\Python*") do (
    if not defined PY (
        "%%~fD\python.exe" -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY="%%~fD\python.exe""
    )
)
if defined PY exit /b 0
for /d %%D in ("C:/Python*") do (
    if not defined PY (
        "%%~fD\python.exe" -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY="%%~fD\python.exe""
    )
)
if defined PY exit /b 0
if exist "C:/Program Files/python/python.exe" (
    "C:/Program Files/python/python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY="C:/Program Files/python/python.exe""
)
exit /b 0

:install_python
set "PYVER=3.12.10"
set "PYINST=%TEMP%\python-%PYVER%-amd64.exe"
call :download_file "%PYINST%" "https://registry.npmmirror.com/-/binary/python/%PYVER%/python-%PYVER%-amd64.exe" "https://mirrors.huaweicloud.com/python/%PYVER%/python-%PYVER%-amd64.exe" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
if errorlevel 1 (
    echo [python] Installer download failed on all mirrors.
    exit /b 1
)
echo [python] Installing Python %PYVER% silently (per-user, 1-2 minutes)...
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=0 /norestart
set "IRC=%errorlevel%"
if "%IRC%"=="0" (
    echo [python] Python %PYVER% installed.
    del /q "%PYINST%" >nul 2>nul
    exit /b 0
)
if "%IRC%"=="3010" (
    echo [python] Python %PYVER% installed, restart recommended.
    del /q "%PYINST%" >nul 2>nul
    exit /b 0
)
echo [python] Silent install failed, exit code %IRC%.
echo          Run the installer manually and enable "Add Python to PATH".
del /q "%PYINST%" >nul 2>nul
exit /b 1

:download_file
rem %1 = save path, %2 and beyond = candidate URLs tried in order.
rem An existing .part smaller than 1 MB is a broken page, not a resume point.
set "DF_DEST=%~1"
if exist "%DF_DEST%" exit /b 0
if exist "%DF_DEST%.part" (
    for %%A in ("%DF_DEST%.part") do if %%~zA LSS 1000000 del /q "%DF_DEST%.part" >nul 2>nul
)
shift
:df_next
if "%~1"=="" (
    echo [python] All download sources failed for %DF_DEST%
    exit /b 1
)
echo [python] Trying: %~1
curl.exe -L --fail --retry 2 --retry-delay 2 --connect-timeout 20 -o "%DF_DEST%.part" "%~1"
if not errorlevel 1 (
    for %%A in ("%DF_DEST%.part") do (
        if %%~zA GEQ 1000000 (
            move /y "%DF_DEST%.part" "%DF_DEST%" >nul
            exit /b 0
        )
    )
)
del /q "%DF_DEST%.part" >nul 2>nul
shift
goto :df_next

rem ===== 依赖下载相关子程序 =====

:ensure_deps
set "MIRRORS=https://gh-proxy.com/ https://ghfast.top/ https://ghproxy.net/"
if defined WARP_GH_MIRROR set "MIRRORS=%WARP_GH_MIRROR%"
if not exist "bin" mkdir "bin" >nul 2>nul

call :ensure_zen
if errorlevel 1 exit /b 1
call :ensure_usque
if errorlevel 1 exit /b 1
call :ensure_warp_plus
if errorlevel 1 exit /b 1
call :ensure_opencode
if errorlevel 1 exit /b 1
exit /b 0

:ensure_zen
if exist "bin\zen-gate.exe" exit /b 0
echo [deps] 缺少 bin\zen-gate.exe，开始下载...
call :download "https://github.com/LAGcomcom/zen-gate/releases/download/v1.10.0/zen-gate.exe" "bin\zen-gate.exe"
exit /b %errorlevel%

:ensure_usque
if exist "bin\usque.exe" exit /b 0
echo [deps] 缺少 bin\usque.exe，开始下载...
set "DL_ZIP=%TEMP%\warp-usque.zip"
call :download "https://github.com/Diniboy1123/usque/releases/download/v4.2.1/usque_4.2.1_windows_amd64.zip" "%DL_ZIP%"
if errorlevel 1 exit /b 1
call :extract_exe "%DL_ZIP%" "usque*.exe" "bin\usque.exe"
set "RC=%errorlevel%"
del /q "%DL_ZIP%" >nul 2>nul
exit /b %RC%

:ensure_warp_plus
if exist "bin\warp-plus.exe" exit /b 0
echo [deps] 缺少 bin\warp-plus.exe，开始下载...
set "DL_ZIP=%TEMP%\warp-plus.zip"
call :download "https://github.com/bepass-org/warp-plus/releases/download/v1.2.6/warp-plus_windows-amd64.zip" "%DL_ZIP%"
if errorlevel 1 exit /b 1
call :extract_exe "%DL_ZIP%" "warp-plus*.exe" "bin\warp-plus.exe"
set "RC=%errorlevel%"
del /q "%DL_ZIP%" >nul 2>nul
exit /b %RC%

:ensure_opencode
if exist "bin\opencode.exe" exit /b 0
echo [deps] 缺少 bin\opencode.exe，开始下载（约 122 MB，请耐心等待）...
set "DL_ZIP=%TEMP%\warp-opencode.zip"
call :download "https://github.com/anomalyco/opencode/releases/download/v2.2.5/opencode-windows-x64.zip" "%DL_ZIP%"
if errorlevel 1 exit /b 1
call :extract_exe "%DL_ZIP%" "opencode*.exe" "bin\opencode.exe"
set "RC=%errorlevel%"
del /q "%DL_ZIP%" >nul 2>nul
exit /b %RC%

:download
rem %1 = 官方 URL，%2 = 保存路径
set "DL_URL=%~1"
set "DL_DEST=%~2"
if exist "%DL_DEST%" exit /b 0

where curl.exe >nul 2>nul
if errorlevel 1 (
    echo [deps] 未找到 curl.exe，无法自动下载。
    echo        Windows 10 1803 及以上系统自带该工具，请升级系统或手动放置文件：
    echo        %DL_DEST%
    exit /b 1
)

rem 太小的 .part 基本是错误页面或空文件，不能续传，直接丢弃。
if exist "%DL_DEST%.part" (
    for %%A in ("%DL_DEST%.part") do if %%~zA LSS 1000000 del /q "%DL_DEST%.part" >nul 2>nul
)

for %%M in (%MIRRORS%) do (
    if not exist "%DL_DEST%" (
        echo [deps] 尝试镜像：%%M
        rem 有可用的 .part 时先尝试断点续传，失败再从头下载。
        if exist "%DL_DEST%.part" (
            curl.exe -L --fail --retry 1 --retry-delay 2 --connect-timeout 20 -C - -o "%DL_DEST%.part" "%%M%DL_URL%"
            if errorlevel 1 del /q "%DL_DEST%.part" >nul 2>nul
        )
        if not exist "%DL_DEST%.part" (
            curl.exe -L --fail --retry 2 --retry-delay 2 --connect-timeout 20 -o "%DL_DEST%.part" "%%M%DL_URL%"
        )
        if exist "%DL_DEST%.part" (
            for %%A in ("%DL_DEST%.part") do if %%~zA GEQ 1000000 move /y "%DL_DEST%.part" "%DL_DEST%" >nul
        )
        if exist "%DL_DEST%.part" del /q "%DL_DEST%.part" >nul 2>nul
    )
)
if exist "%DL_DEST%" exit /b 0
echo [deps] 下载失败：%DL_URL%
echo        已尝试的镜像：%MIRRORS%
exit /b 1

:extract_exe
rem %1 = zip 文件，%2 = 文件名通配符，%3 = 目标路径
set "ZIP=%~1"
set "NAME=%~2"
set "DEST=%~3"
set "EXDIR=%TEMP%\warp-dep-%RANDOM%%RANDOM%"
mkdir "%EXDIR%" >nul 2>nul
tar.exe -xf "%ZIP%" -C "%EXDIR%" >nul 2>nul
set "SRC="
for /f "delims=" %%F in ('dir /b /s /o-s "%EXDIR%\%NAME%" 2^>nul') do if not defined SRC set "SRC=%%~fF"
if not defined SRC (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%EXDIR%' -Force" >nul 2>nul
    for /f "delims=" %%F in ('dir /b /s /o-s "%EXDIR%\%NAME%" 2^>nul') do if not defined SRC set "SRC=%%~fF"
)
if not defined SRC (
    echo [deps] 压缩包内未找到 %NAME%
    rmdir /s /q "%EXDIR%" >nul 2>nul
    exit /b 1
)
move /y "%SRC%" "%DEST%" >nul
set "RC=%errorlevel%"
rmdir /s /q "%EXDIR%" >nul 2>nul
if not "%RC%"=="0" (
    echo [deps] 无法写入 %DEST%
    exit /b 1
)
exit /b 0
