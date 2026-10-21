@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "SOURCE=%SCRIPT_DIR%ToolFB_1.9.9.zip"
if not exist "%SOURCE%" set "SOURCE=%SCRIPT_DIR%..\ToolFB_1.9.9.zip"
if not exist "%SOURCE%" set "SOURCE=%USERPROFILE%\Downloads\ToolFB_1.9.9.zip"
if not exist "%SOURCE%" (
  echo Cannot find ToolFB_1.9.9.zip. Put the original ZIP beside build_windows.cmd and run it again.
  pause
  exit /b 1
)

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  py -3 "%SCRIPT_DIR%build_from_original.py" "%SOURCE%"
  goto :result
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install it, then run build_windows.cmd again.
  pause
  exit /b 1
)
python "%SCRIPT_DIR%build_from_original.py" "%SOURCE%"

:result
if errorlevel 1 (
  echo Build failed. Read the error above.
  pause
  exit /b 1
)
echo Extension is ready. In chrome://extensions choose Load unpacked and select:
echo "%SCRIPT_DIR%dist\XMETA_PageBatch_1.9.9"
pause
