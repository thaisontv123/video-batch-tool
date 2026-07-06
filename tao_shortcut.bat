@echo off
REM ============================================================
REM  Tao shortcut "Render Video Hang Loat" ra man hinh Desktop
REM  Chay file nay 1 lan sau khi tai/giai nen tool.
REM ============================================================
setlocal
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

REM --- Tim pythonw.exe (chay khong hien cua so console) ---
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not defined PYW (
  echo [LOI] Khong tim thay Python. Hay cai Python truoc: https://www.python.org/downloads/
  echo Nho tich "Add Python to PATH" khi cai.
  pause
  exit /b 1
)

set "ICON=%DIR%\icon.ico"
set "TARGET=%DIR%\app.py"

powershell -NoProfile -Command ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$p=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Render Video Hang Loat.lnk';" ^
  "$s=$w.CreateShortcut($p);" ^
  "$s.TargetPath='%PYW%';" ^
  "$s.Arguments='\"%TARGET%\"';" ^
  "$s.WorkingDirectory='%DIR%';" ^
  "$s.IconLocation='%ICON%';" ^
  "$s.Description='Cong cu Render Video Hang Loat';" ^
  "$s.Save()"

if exist "%USERPROFILE%\Desktop\Render Video Hang Loat.lnk" (
  echo [OK] Da tao shortcut "Render Video Hang Loat" tren Desktop.
) else (
  echo [OK] Da chay xong. Kiem tra shortcut tren Desktop.
)
echo.
echo Luu y: can cai ffmpeg va them vao PATH thi tool moi render duoc (xem README).
pause
