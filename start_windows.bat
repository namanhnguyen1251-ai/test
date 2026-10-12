@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto failure
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failure
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failure
exit /b 0
:failure
echo.
echo Khong the khoi dong. Xem thong bao loi o tren.
echo Can cai Python 3.10 tro len va bat tuy chon Python Launcher.
pause
exit /b 1
