@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "UV_VERSION=0.10.2"
set "STEP_LOG=%~1"
if not defined STEP_LOG set "STEP_LOG=%TEMP%\windows-supporter-uv-bootstrap.log"

set "UV_REQUIREMENTS=%WINDOWS_SUPPORTER_UV_BOOTSTRAP_REQUIREMENTS%"
if not defined UV_REQUIREMENTS set "UV_REQUIREMENTS=%~dp0uv-bootstrap-requirements.txt"
if not exist "%UV_REQUIREMENTS%" goto requirements_missing

set "BUILD_PYTHON=%WINDOWS_SUPPORTER_BUILD_PYTHON%"
if defined BUILD_PYTHON goto validate_python
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>NUL') do if not defined BUILD_PYTHON set "BUILD_PYTHON=%%P"
if defined BUILD_PYTHON goto validate_python
for /f "delims=" %%P in ('py -3.14 -c "import sys; print(sys.executable)" 2^>NUL') do if not defined BUILD_PYTHON set "BUILD_PYTHON=%%P"
if not defined BUILD_PYTHON goto python_missing

:validate_python
if not exist "%BUILD_PYTHON%" goto python_missing
"%BUILD_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 14) else 1)" >> "%STEP_LOG%" 2>&1
if errorlevel 1 goto python_unsupported

set "UV_CACHE_ROOT=%WINDOWS_SUPPORTER_UV_CACHE_ROOT%"
if not defined UV_CACHE_ROOT if defined LOCALAPPDATA set "UV_CACHE_ROOT=%LOCALAPPDATA%\windows-supporter\build-tools\uv"
if not defined UV_CACHE_ROOT set "UV_CACHE_ROOT=%TEMP%\windows-supporter\build-tools\uv"
set "UV_INSTALL_DIR=%UV_CACHE_ROOT%\%UV_VERSION%"
set "UV_EXE=%UV_INSTALL_DIR%\Scripts\uv.exe"

call :read_uv_version "%UV_EXE%"
if "%DETECTED_UV_VERSION%"=="%UV_VERSION%" goto success

if not exist "%UV_CACHE_ROOT%" mkdir "%UV_CACHE_ROOT%" >> "%STEP_LOG%" 2>&1
if errorlevel 1 goto cache_create_failed

set "UV_STAGING_DIR=%UV_INSTALL_DIR%.staging-%RANDOM%%RANDOM%"
if exist "%UV_STAGING_DIR%" rmdir /S /Q "%UV_STAGING_DIR%" >> "%STEP_LOG%" 2>&1
"%BUILD_PYTHON%" -m venv "%UV_STAGING_DIR%" >> "%STEP_LOG%" 2>&1
if errorlevel 1 goto venv_failed

set "UV_STAGING_PYTHON=%UV_STAGING_DIR%\Scripts\python.exe"
set "UV_STAGING_EXE=%UV_STAGING_DIR%\Scripts\uv.exe"
"%UV_STAGING_PYTHON%" -m pip install --disable-pip-version-check --no-input --only-binary=:all: --require-hashes -r "%UV_REQUIREMENTS%" >> "%STEP_LOG%" 2>&1
if errorlevel 1 goto install_failed

call :read_uv_version "%UV_STAGING_EXE%"
if not "%DETECTED_UV_VERSION%"=="%UV_VERSION%" goto version_failed

if exist "%UV_INSTALL_DIR%" rmdir /S /Q "%UV_INSTALL_DIR%" >> "%STEP_LOG%" 2>&1
if exist "%UV_INSTALL_DIR%" goto cache_replace_failed
move /Y "%UV_STAGING_DIR%" "%UV_INSTALL_DIR%" >> "%STEP_LOG%" 2>&1
if errorlevel 1 goto cache_replace_failed

call :read_uv_version "%UV_EXE%"
if not "%DETECTED_UV_VERSION%"=="%UV_VERSION%" goto version_failed

:success
endlocal & set "WINDOWS_SUPPORTER_BUILD_PYTHON=%BUILD_PYTHON%" & set "WINDOWS_SUPPORTER_UV_EXE=%UV_EXE%" & exit /b 0

:requirements_missing
set "UV_ERROR=uv bootstrap requirements file was not found: %UV_REQUIREMENTS%"
goto failed

:python_missing
set "UV_ERROR=Python 3.14 or newer was not found. Install Python and ensure python.exe or py.exe is available."
goto failed

:python_unsupported
set "UV_ERROR=The detected Python is older than 3.14: %BUILD_PYTHON%"
goto failed

:cache_create_failed
set "UV_ERROR=Failed to create the uv build-tool cache: %UV_CACHE_ROOT%"
goto failed

:venv_failed
set "UV_ERROR=Failed to create the isolated uv bootstrap environment."
goto failed

:install_failed
set "UV_ERROR=Failed to install the pinned uv build tool. Check network and certificate settings."
goto failed

:version_failed
set "UV_ERROR=The bootstrapped uv executable did not report the required version %UV_VERSION%."
goto failed

:cache_replace_failed
set "UV_ERROR=Failed to promote the verified uv build tool into the user-local cache."
goto failed

:failed
>> "%STEP_LOG%" echo %UV_ERROR%
if defined UV_STAGING_DIR if exist "%UV_STAGING_DIR%" rmdir /S /Q "%UV_STAGING_DIR%" >> "%STEP_LOG%" 2>&1
endlocal & exit /b 1

:read_uv_version
set "DETECTED_UV_VERSION="
if not exist "%~1" exit /b 0
for /f "tokens=2" %%V in ('"%~1" --version 2^>NUL') do if not defined DETECTED_UV_VERSION set "DETECTED_UV_VERSION=%%V"
exit /b 0
