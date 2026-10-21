@echo off
rem Builds FileOrganizer on Windows. Result: dist\FileOrganizer\FileOrganizer.exe
rem If Inno Setup 6 is installed, also builds installer_out\FileOrganizer-Setup.exe
setlocal
cd /d "%~dp0"
python -m venv .venv || goto :fail
call .venv\Scripts\activate || goto :fail
python -m pip install --upgrade pip || goto :fail
pip install -r requirements.txt pillow-heif pyinstaller || goto :fail
python -m unittest discover -s tests || goto :fail
pyinstaller --noconfirm --clean --windowed --name FileOrganizer ^
  --collect-data geonamescache --collect-all pillow_heif run_organizer.py || goto :fail
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% installer.iss || goto :fail
  echo.
  echo Installer: %cd%\installer_out\FileOrganizer-Setup.exe
) else (
  echo.
  echo Built: %cd%\dist\FileOrganizer\FileOrganizer.exe  ^(install Inno Setup 6 to also make an installer^)
)
goto :eof
:fail
echo.
echo BUILD FAILED. Scroll up for the first error.
exit /b 1
