@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys;sys.exit(sys.version_info < (3,9))" >nul 2>nul
if not errorlevel 1 (
    py -3 installer.py %*
    goto done
)
python -c "import sys;sys.exit(sys.version_info < (3,9))" >nul 2>nul
if not errorlevel 1 (
    python installer.py %*
    goto done
)
echo Python is missing or too old. Install Python 3.12 64-bit from https://www.python.org/downloads/
:done
pause
