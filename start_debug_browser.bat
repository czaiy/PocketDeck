@echo off
setlocal
chcp 65001 >nul 2>&1

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "CHROME86=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "EDGE64=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
set "UDD=%~dp0tests\chrome-profile"
set "PORT=9222"
set "START_URL=%~1"
if "%START_URL%"=="" set "START_URL=https://www.google.com"

if not exist "%UDD%" mkdir "%UDD%"

echo [KZQ] debug browser
echo [KZQ] port=%PORT%
echo [KZQ] profile=%UDD%
echo [KZQ] url=%START_URL%
echo.

if exist "%CHROME%" goto use_chrome
if exist "%CHROME86%" goto use_chrome86
if exist "%EDGE%" goto use_edge
if exist "%EDGE64%" goto use_edge64
echo [KZQ] ERROR: Chrome or Edge not found, edit this script.
pause
exit /b 1

:use_chrome
echo [KZQ] exe=%CHROME%
start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%UDD%" --no-first-run --no-default-browser-check --disable-features=Translate --window-size=1440,960 "%START_URL%"
goto done

:use_chrome86
echo [KZQ] exe=%CHROME86%
start "" "%CHROME86%" --remote-debugging-port=%PORT% --user-data-dir="%UDD%" --no-first-run --no-default-browser-check --disable-features=Translate --window-size=1440,960 "%START_URL%"
goto done

:use_edge
echo [KZQ] exe=%EDGE%
start "" "%EDGE%" --remote-debugging-port=%PORT% --user-data-dir="%UDD%" --no-first-run --no-default-browser-check --disable-features=Translate --window-size=1440,960 "%START_URL%"
goto done

:use_edge64
echo [KZQ] exe=%EDGE64%
start "" "%EDGE64%" --remote-debugging-port=%PORT% --user-data-dir="%UDD%" --no-first-run --no-default-browser-check --disable-features=Translate --window-size=1440,960 "%START_URL%"
goto done

:done
echo.
echo [KZQ] started. check port in 2-3s:
echo       curl http://127.0.0.1:%PORT%/json/version
echo.
echo [KZQ] if the browser exits immediately:
echo       1) this profile dir is already used by another Chrome instance
echo          - close the window opened by this script before
echo       2) path contains non-ASCII chars - move this folder to an ASCII path
echo.
if not defined KZQ_NO_PAUSE pause
