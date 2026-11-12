@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem opencode launcher with a proxy pointing at WARP Control.
rem
rem By default the proxy is http://127.0.0.1:<http_port> where <http_port> is
rem read from config.json (7891 when the file or the key is missing).
rem
rem Override the address with:  set OPENCODE_PROXY=http://127.0.0.1:1080
rem ---------------------------------------------------------------------------

set "PROXY_PORT="
for /f "tokens=2 delims=:," %%P in ('findstr /i http_port "config\config.json" 2^>nul') do set "PROXY_PORT=%%P"
if defined PROXY_PORT set "PROXY_PORT=%PROXY_PORT: =%"
if defined PROXY_PORT set "PROXY_PORT=%PROXY_PORT:}=%"
if defined PROXY_PORT set "PROXY_PORT=%PROXY_PORT:"=%"
if not defined PROXY_PORT set "PROXY_PORT=7891"

if defined OPENCODE_PROXY (
    set "PROXY_URL=%OPENCODE_PROXY%"
) else (
    set "PROXY_URL=http://127.0.0.1:%PROXY_PORT%"
)

rem opencode runs on Bun: set both upper- and lower-case variants, plus ALL_PROXY.
set "HTTP_PROXY=%PROXY_URL%"
set "HTTPS_PROXY=%PROXY_URL%"
set "ALL_PROXY=%PROXY_URL%"
set "http_proxy=%PROXY_URL%"
set "https_proxy=%PROXY_URL%"
set "all_proxy=%PROXY_URL%"

rem Keep local traffic (e.g. the proxy itself) out of the proxy.
set "NO_PROXY=localhost,127.0.0.1,::1,0.0.0.0"
set "no_proxy=%NO_PROXY%"

rem Warn, but still start, when WARP Control is not listening yet.
if not defined OPENCODE_PROXY (
    netstat -ano | findstr /r ":%PROXY_PORT%.*LISTENING" >nul 2>nul
    if errorlevel 1 (
        echo [warn] Nothing is listening on 127.0.0.1:%PROXY_PORT%.
        echo [warn] Run start.bat ^(WARP Control^) first, or set OPENCODE_PROXY.
        echo.
    )
)

rem All project paths are relative to the script directory (set by cd above),
rem so the folder can be renamed / moved / copied without breaking anything.
rem opencode 的项目配置也统一放在 config 目录下。
set "OPENCODE_CONFIG=config\opencode.json"
if not exist "%OPENCODE_CONFIG%" (
    echo [warn] Missing %OPENCODE_CONFIG% - opencode will use defaults.
    echo.
)

echo Using proxy: %PROXY_URL%
echo Using config: %OPENCODE_CONFIG%
echo.

bin\opencode.exe %*
set "RC=%errorlevel%"
endlocal & exit /b %RC%
