@echo off
setlocal
cd /d "%~dp0"

rem --- find a compatible Python (3.11 or 3.12) ---
set "PY="
py -3.11 --version >nul 2>nul && set "PY=py -3.11"
if not defined PY (py -3.12 --version >nul 2>nul && set "PY=py -3.12")
if not defined PY set "PY=python"
echo Using: %PY%

%PY% -m venv .venv || goto :err
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller pip-licenses || goto :err

pyinstaller --noconfirm --onefile --windowed --name GloveMouse ^
  --collect-data mediapipe --collect-binaries mediapipe glove_mouse.py || goto :err
python make_release.py || goto :err

echo.
echo Done! App: %cd%\dist\GloveMouse.exe
echo Shareable package: %cd%\release\GloveMouse-windows.zip
pause
exit /b 0

:err
echo.
echo Build failed - see the messages above.
pause
exit /b 1
