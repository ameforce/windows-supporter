@ECHO OFF
setlocal EnableExtensions EnableDelayedExpansion

set "PROJECT_ROOT=%~1"
if not defined PROJECT_ROOT set "PROJECT_ROOT=%CD%"
set "VENV_DIR=%PROJECT_ROOT%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "PYVENV_CFG=%VENV_DIR%\pyvenv.cfg"
set "VENV_HOME="

if not exist "%VENV_DIR%" exit /b 0

if not exist "%VENV_PYTHON%" goto remove_stale_venv
if not exist "%PYVENV_CFG%" goto remove_stale_venv

if exist "%PYVENV_CFG%" (
  for /f "usebackq tokens=1* delims==" %%A in ("%PYVENV_CFG%") do (
    if /I "%%~A"=="home " set "VENV_HOME=%%~B"
    if /I "%%~A"=="home" set "VENV_HOME=%%~B"
  )
)

if not defined VENV_HOME goto env_ready
for /f "tokens=* delims= " %%A in ("!VENV_HOME!") do set "VENV_HOME=%%~A"
if exist "!VENV_HOME!\python.exe" goto env_ready
goto remove_stale_venv

:remove_stale_venv
echo Detected stale virtual environment at "%VENV_DIR%". Removing it so uv can recreate it.
call "%~dp0remove_path_with_retry.bat" "%VENV_DIR%" 5 1 > NUL
if exist "%VENV_DIR%" exit /b 1
exit /b 0

:env_ready
exit /b 0
