@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set LOG=test_output.log
echo === Coinmania iOS smoke test === > "%LOG%"
echo Started at %DATE% %TIME% >> "%LOG%"
echo. >> "%LOG%"

REM --- Find Python ---
set PY=
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set PY=python
) else (
    where py >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        set PY=py -3
    )
)

if "%PY%"=="" (
    echo ERROR: Python not found. >> "%LOG%"
    echo. >> "%LOG%"
    echo Install Python 3.10+ from https://www.python.org/downloads/ >> "%LOG%"
    echo or via the Microsoft Store ^(search "python 3"^), then double-click this .bat again. >> "%LOG%"
    type "%LOG%"
    echo.
    pause
    exit /b 1
)

%PY% --version >> "%LOG%" 2>&1

REM --- Find the .p8 ---
set P8=
if exist "%~dp0AuthKey_FRA3AGWC39.p8" set "P8=%~dp0AuthKey_FRA3AGWC39.p8"
if "%P8%"=="" if exist "%USERPROFILE%\Downloads\AuthKey_FRA3AGWC39.p8" set "P8=%USERPROFILE%\Downloads\AuthKey_FRA3AGWC39.p8"

if "%P8%"=="" (
    echo. >> "%LOG%"
    echo ERROR: AuthKey_FRA3AGWC39.p8 not found in this folder or in your Downloads. >> "%LOG%"
    echo Move it into this folder and double-click again. >> "%LOG%"
    type "%LOG%"
    echo.
    pause
    exit /b 1
)

echo Using .p8: %P8% >> "%LOG%"

REM --- Install deps quietly ---
echo. >> "%LOG%"
echo Installing httpx PyJWT cryptography (user-level)... >> "%LOG%"
%PY% -m pip install --user --quiet --disable-pip-version-check httpx PyJWT cryptography >> "%LOG%" 2>&1

REM --- Run the test ---
echo. >> "%LOG%"
echo === Test output === >> "%LOG%"
%PY% test_apple.py "%P8%" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo === Done === >> "%LOG%"
type "%LOG%"
echo.
echo Output also saved to test_output.log in this folder.
pause
