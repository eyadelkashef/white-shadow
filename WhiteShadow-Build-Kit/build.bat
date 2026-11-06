@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title White Shadow - one-click build
set "LOG=%~dp0build-log.txt"
> "%LOG%" echo White Shadow build log - %date% %time%

echo ============================================================
echo   WHITE SHADOW - one-click build: the app + the Windows setup file
echo   You do not need to install anything first. This script gets
echo   what it needs (Python 3.12 and Inno Setup) by itself.
echo   Needs internet. Takes about 5-10 minutes. Log: build-log.txt
echo ============================================================
echo.

if not exist "WhiteShadowLauncher.py" goto :notextracted
if not exist "installer.iss" goto :notextracted
if not exist "icon.ico" goto :notextracted

rem ---------------------------------------------------------- 1. Python 3.12
call :findpy
if defined PY goto :havepy
echo [1/5] Python 3.12 is not installed - installing it just for your user account (about a minute)...
winget --version >nul 2>&1
if errorlevel 1 goto :nopython
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
call :findpy
if not defined PY goto :nopython
:havepy
echo [1/5] Python 3.12 found.

rem ---------------------------------------------------------- 2. private build environment
echo [2/5] Preparing the build tools...
if exist ".buildenv" rmdir /s /q ".buildenv"
"%PY%" -m venv ".buildenv" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
set "VPY=.buildenv\Scripts\python.exe"
%VPY% -m pip install --upgrade pip >> "%LOG%" 2>&1
%VPY% -m pip install pyinstaller pillow certifi >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
set "COLLECT="
%VPY% -m pip install pywebview >> "%LOG%" 2>&1
if errorlevel 1 goto :nowebview
set "COLLECT=--collect-all webview"
goto :buildapp
:nowebview
echo       The native window component could not be installed; the app will open in a browser app window instead.

rem ---------------------------------------------------------- 3. build the app
:buildapp
echo [3/5] Building the app - please wait, this is the slow part...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
%VPY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name WhiteShadow --icon icon.ico %COLLECT% WhiteShadowLauncher.py >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
if not exist "dist\WhiteShadow.exe" goto :noexe
move /y "dist\WhiteShadow.exe" "dist\WhiteShadow-windows.exe" >nul

rem ---------------------------------------------------------- 4. the setup file (Inno Setup)
echo [4/5] Creating the setup file...
set "APPVER=1.0.0"
for /f "delims=" %%V in ('%VPY% get_version.py') do set "APPVER=%%V"
call :findiscc
if defined ISCC goto :haveinno
echo       Installing Inno Setup (one time)...
winget --version >nul 2>&1
if errorlevel 1 goto :noinno
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
call :findiscc
if not defined ISCC goto :noinno
:haveinno
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile 'dist\MicrosoftEdgeWebview2Setup.exe' } catch { }" >> "%LOG%" 2>&1
"%ISCC%" /DAppVer=%APPVER% installer.iss >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
if not exist "dist\WhiteShadow-Setup.exe" goto :fail
goto :publish

:noinno
echo.
echo       Inno Setup could not be installed automatically, so only the portable app was built.
echo       Install it from https://jrsoftware.org/isdl.php and run build.bat again to get the setup file.
start "" "https://jrsoftware.org/isdl.php"

rem ---------------------------------------------------------- 5. collect the results
:publish
echo [5/5] Collecting the finished files...
if not exist "OUTPUT" mkdir "OUTPUT"
if exist "dist\WhiteShadow-Setup.exe" copy /y "dist\WhiteShadow-Setup.exe" "OUTPUT\" >nul
copy /y "dist\WhiteShadow-windows.exe" "OUTPUT\" >nul
%VPY% make_downloads.py >> "%LOG%" 2>&1
echo.
echo ============================================================
echo   DONE!
echo.
echo   The folder OUTPUT is opening. In it:
echo     WhiteShadow-Setup.exe     = the installer your players download and run
echo     WhiteShadow-windows.exe   = the portable app (runs without installing)
echo.
echo   Website: the folder "site" is ready to upload to your web host -
echo   its Download button already serves the setup file.
echo ============================================================
start "" explorer "%~dp0OUTPUT"
pause
exit /b 0

rem ---------------------------------------------------------- helpers
:findpy
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"
if not defined PY for /f "delims=" %%P in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
exit /b 0

:findiscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
exit /b 0

rem ---------------------------------------------------------- problems
:notextracted
echo.
echo   Some files are missing next to build.bat.
echo   Right-click the ZIP file, choose "Extract All...", and run build.bat from the extracted folder.
echo   (Running it from inside the zip does not work.)
pause
exit /b 1

:nopython
echo.
echo   Python 3.12 could not be installed automatically.
echo   Please install it from https://www.python.org/downloads/release/python-3129/
echo   (choose "Windows installer 64-bit"), then run build.bat again.
start "" "https://www.python.org/downloads/release/python-3129/"
pause
exit /b 1

:noexe
echo.
echo   The build finished but WhiteShadow.exe is missing. Windows Security may have removed it.
echo   Open Windows Security - Virus and threat protection - Protection history, restore it,
echo   add this folder as an exclusion, then run build.bat again.
pause
exit /b 1

:fail
echo.
echo ============================================================
echo   The build stopped. The last lines of the log:
echo ============================================================
powershell -NoProfile -Command "Get-Content -Tail 30 '%LOG%'"
echo.
echo   The full log is in build-log.txt - send it to me and I will fix it.
start "" notepad "%LOG%"
pause
exit /b 1
