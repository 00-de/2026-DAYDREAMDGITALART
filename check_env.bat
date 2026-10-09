@echo off
rem ============================================================
rem  DayDream Plus Digital Mosaic - environment check & tests
rem  Double-click this file. (Python must be installed first)
rem ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

echo [1/3] Installing libraries...
%PY% -m pip install --upgrade pip >nul
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo [2/3] Checking environment...
%PY% check_env.py
if errorlevel 1 goto :error

echo.
echo [3/3] Running tests...
%PY% -m unittest discover -s tests -v
if errorlevel 1 goto :error

echo.
echo ==== ALL OK ====
pause
exit /b 0

:error
echo.
echo ==== ERROR : please send a screenshot of this window ====
pause
exit /b 1
