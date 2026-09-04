@ECHO OFF
setlocal EnableExtensions DisableDelayedExpansion

REM Variables
set "EXE_BASE=windows-supporter"
set "EXE_NAME=%EXE_BASE%.exe"
set "MAIN_SOURCE=main.py"
set "CURRENT_DIR=%~dp0"
set "ROOT_EXE=%CURRENT_DIR%%EXE_NAME%"
set "BUILD_GENERATED_DIR=build\generated"
set "BUILD_INFO_MODULE=%BUILD_GENERATED_DIR%\windows_supporter_build_info.py"
set "VERSION_FILE=%BUILD_GENERATED_DIR%\%EXE_BASE%-version-info.txt"
set "STEP_LOG=%TEMP%\%EXE_BASE%-build-%RANDOM%%RANDOM%.log"
set "ARTIFACT_ONLY=0"
if /I "%WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY%"=="1" set "ARTIFACT_ONLY=1"
set "EMIT_STEP_LOG=0"
if /I "%WINDOWS_SUPPORTER_EMIT_STEP_LOG%"=="1" set "EMIT_STEP_LOG=1"
if "%EMIT_STEP_LOG%"=="1" echo WINDOWS_SUPPORTER_STEP_LOG=%STEP_LOG%

REM Switch to repo root
cd /d "%CURRENT_DIR%"
if errorlevel 1 (
  echo Failed to change working directory to "%CURRENT_DIR%"
  exit /b 1
)

echo Build phase leaves the installed %EXE_NAME% process unchanged until candidate validation passes.

REM Stop leftover PyInstaller worker processes from previous failed builds
echo | set /p="Stopping stale PyInstaller workers..."
call :clear_log
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\stop_stale_pyinstaller_workers.ps1" "%CURRENT_DIR:~0,-1%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Failed to stop stale PyInstaller worker processes.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Remove stale virtual environment if its base Python path is no longer valid
call "tools\ensure_venv_ready.bat" "%CURRENT_DIR:~0,-1%"
if errorlevel 1 (
  echo Failed to repair the project virtual environment.
  exit /b 1
)

REM Bootstrap the pinned uv build tool without modifying the global PATH
echo | set /p="Preparing pinned uv build tool..."
call :clear_log
call "tools\ensure_uv_ready.bat" "%STEP_LOG%"
if errorlevel 1 (
  echo Failure
  echo Failed to prepare the pinned uv build tool.
  call :print_log
  exit /b 1
)
echo [ Success !! ]
set "UV_PYTHON=%WINDOWS_SUPPORTER_BUILD_PYTHON%"
set "UV_PYTHON_DOWNLOADS=never"

REM Sync uv environment (PyInstaller is in the build extra)
echo | set /p="Syncing uv environment..."
call :clear_log
"%WINDOWS_SUPPORTER_UV_EXE%" sync --locked --extra build > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo uv sync failed. Check the locked dependencies and network settings.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Install bundled Playwright browser runtime into package-local path
echo | set /p="Preparing bundled Playwright Chromium runtime..."
call :clear_log
set "PLAYWRIGHT_BROWSERS_PATH=0"
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python -m playwright install chromium > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Playwright Chromium runtime install failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Remove stale PyInstaller byproducts before starting a new build
echo | set /p="Cleaning prior PyInstaller byproducts..."
call :clear_log
call :remove_pyinstaller_byproducts " before build"
if errorlevel 1 exit /b 1
echo [ Success !! ]

REM Generate build-time version metadata from Git tags and current commit
echo | set /p="Generating version metadata..."
call :clear_log
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python "tools\generate_build_metadata.py" --repo-root "%CURRENT_DIR:~0,-1%" --module-output "%BUILD_INFO_MODULE%" --version-file "%VERSION_FILE%" --exe-name "%EXE_NAME%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Version metadata generation failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Build the executable
echo | set /p="Building %MAIN_SOURCE% to %EXE_NAME%..."
call :clear_log
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python -m PyInstaller -n "%EXE_BASE%" --onefile --noconsole --icon "src\utils\windows_supporter.ico" --version-file "%VERSION_FILE%" --paths "%BUILD_GENERATED_DIR%" --hidden-import windows_supporter_build_info --collect-all playwright --add-data "src\utils\windows_supporter.ico;src\utils" --add-data "src\apps\resources\google_desktop_oauth.json;src\apps\resources" "%MAIN_SOURCE%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo PyInstaller build failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Validate the staged onefile archive before replacing the current executable
echo | set /p="Validating PyInstaller archive..."
call :clear_log
if not exist "dist\%EXE_NAME%" (
  > "%STEP_LOG%" echo Expected build artifact was not found: dist\%EXE_NAME%
  echo Failure
  echo PyInstaller archive validation failed.
  call :print_log
  exit /b 1
)
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python "tools\verify_pyinstaller_archive.py" "dist\%EXE_NAME%" --entry "playwright\driver\node.exe" --entry "src\apps\resources\google_desktop_oauth.json" --match-file ".venv\Lib\site-packages\playwright\driver\node.exe" --match-file "src\apps\resources\google_desktop_oauth.json" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo PyInstaller archive validation failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Exercise the bundled OAuth resource through the frozen importlib.resources loader
echo | set /p="Validating frozen Google Calendar resource loader..."
call :clear_log
"dist\%EXE_NAME%" --google-calendar-resource-smoke > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Frozen Google Calendar resource loader validation failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Exercise frozen multiprocessing re-entry and Windows Job containment
echo | set /p="Validating Codex usage worker boundary..."
call :clear_log
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python "tools\verify_codex_usage_worker_smoke.py" "dist\%EXE_NAME%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Codex usage worker boundary validation failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

if "%ARTIFACT_ONLY%"=="1" (
  echo Artifact-only build complete: dist\%EXE_NAME%
  echo The repo-root executable was not replaced and no application was launched.
  call :clear_log
  endlocal
  exit /b 0
)

REM Deploy only after all candidate validation passes. The helper owns exact-process
REM shutdown, backup, atomic replacement, readiness verification, and rollback.
echo | set /p="Deploying verified %EXE_NAME% transaction..."
call :clear_log
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python "tools\deploy_runtime.py" --candidate "dist\%EXE_NAME%" --target "%ROOT_EXE%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Transactional runtime deployment failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Remove build byproducts
echo | set /p="Remove build byproducts..."
call :clear_log
call :remove_pyinstaller_byproducts ""
if errorlevel 1 exit /b 1
echo [ Success !! ]

call :clear_log
endlocal
exit /b 0

:remove_pyinstaller_byproducts
set "FAILURE_SUFFIX=%~1"
if exist "%EXE_BASE%.spec" (
  call "tools\remove_path_with_retry.bat" "%EXE_BASE%.spec" 15 1 > "%STEP_LOG%" 2>&1
  if exist "%EXE_BASE%.spec" (
    echo Failure
    echo Failed to remove generated spec file%FAILURE_SUFFIX%.
    call :print_log
    exit /b 1
  )
)
if exist "build" (
  call "tools\remove_path_with_retry.bat" "build" 15 1 >> "%STEP_LOG%" 2>&1
  if exist "build" (
    echo Failure
    echo Failed to remove build directory%FAILURE_SUFFIX%.
    call :print_log
    exit /b 1
  )
)
if exist "dist" (
  call "tools\remove_path_with_retry.bat" "dist" 15 1 >> "%STEP_LOG%" 2>&1
  if exist "dist" (
    echo Failure
    echo Failed to remove dist directory%FAILURE_SUFFIX%.
    call :print_log
    exit /b 1
  )
)
call :clear_log
exit /b 0

:clear_log
if exist "%STEP_LOG%" del "%STEP_LOG%" > NUL 2>&1
exit /b 0

:print_log
if exist "%STEP_LOG%" (
  echo ----- begin command log -----
  type "%STEP_LOG%"
  echo ----- end command log -----
)
exit /b 0
