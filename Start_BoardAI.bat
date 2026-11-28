@echo off
setlocal
cd /d "%~dp0"
title Board AI Diagnosis - Launcher

if not exist "Launcher.py" (
  echo [ERROR] Launcher.py was not found next to this file.
  echo Extract the whole zip file to a folder first, then run this file again.
  echo Do not run it from inside the zip file.
  pause
  exit /b 1
)

python -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found.
  echo Install Python 3 and check "Add python.exe to PATH" in the installer.
  pause
  exit /b 1
)

set MISSING=
echo Checking required components...
for %%M in (tkinter PIL cv2 google.genai OpenRTM_aist) do call :check %%M
if defined MISSING (
  echo.
  echo Some components are missing. How to fix:
  echo   tkinter      : reinstall Python with "tcl/tk and IDLE" selected
  echo   PIL          : pip install pillow
  echo   cv2          : pip install opencv-python
  echo   google.genai : pip install google-genai
  echo   OpenRTM_aist : install OpenRTM-aist 2.1.0 with Python support
  echo See the manual, chapter 2.
  pause
  exit /b 1
)

echo.
echo Starting the launcher. Keep this window open. You can minimize it.
python Launcher.py
if errorlevel 1 (
  echo.
  echo [ERROR] The launcher stopped with an error.
  pause
)
endlocal
exit /b 0

:check
python -c "import %1" >nul 2>nul
if errorlevel 1 (
  echo   [NG] %1
  set MISSING=1
) else (
  echo   [OK] %1
)
exit /b 0
