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
set "POST_BUILD_RUN_START_TIMEOUT_SECONDS=60"
set "SKIP_POST_BUILD_RUN=0"
if /I "%WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN%"=="1" set "SKIP_POST_BUILD_RUN=1"

REM Switch to repo root
cd /d "%CURRENT_DIR%"
if errorlevel 1 (
  echo Failed to change working directory to "%CURRENT_DIR%"
  exit /b 1
)

REM Stop the running executable before rebuilding
echo | set /p="Shutting down the running %EXE_NAME% process..."
call :clear_log
taskkill /f /t /im "%EXE_NAME%" > "%STEP_LOG%" 2>&1
set "TASKKILL_ERROR=%ERRORLEVEL%"
if "%TASKKILL_ERROR%"=="0" (
  echo [ Success !! ]
) else if "%TASKKILL_ERROR%"=="128" (
  echo [ Not running ]
) else (
  echo Failure
  echo Failed to stop the running %EXE_NAME% process.
  call :print_log
  exit /b 1
)
call :wait_for_process_stop
if errorlevel 1 (
  echo Failure
  echo %EXE_NAME% is still running after taskkill.
  exit /b 1
)

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

REM Sync uv environment (PyInstaller is in the build extra)
echo | set /p="Syncing uv environment..."
call :clear_log
uv sync --extra build > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo uv sync failed. Please check that uv is installed and available in PATH.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Install bundled Playwright browser runtime into package-local path
echo | set /p="Preparing bundled Playwright Chromium runtime..."
call :clear_log
set "PLAYWRIGHT_BROWSERS_PATH=0"
uv run python -m playwright install chromium > "%STEP_LOG%" 2>&1
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
uv run python "tools\generate_build_metadata.py" --repo-root "%CURRENT_DIR:~0,-1%" --module-output "%BUILD_INFO_MODULE%" --version-file "%VERSION_FILE%" --exe-name "%EXE_NAME%" > "%STEP_LOG%" 2>&1
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
uv run python -m PyInstaller -n "%EXE_BASE%" --onefile --noconsole --icon "src\utils\windows_supporter.ico" --version-file "%VERSION_FILE%" --paths "%BUILD_GENERATED_DIR%" --hidden-import windows_supporter_build_info --collect-all playwright --add-data "src\utils\windows_supporter.ico;src\utils" "%MAIN_SOURCE%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo PyInstaller build failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Promote the built artifact to the repo root
echo | set /p="Moving %EXE_NAME%..."
call :clear_log
if not exist "dist\%EXE_NAME%" (
  > "%STEP_LOG%" echo Expected build artifact was not found: dist\%EXE_NAME%
  echo Failure
  echo Built artifact move failed.
  call :print_log
  exit /b 1
)
move /Y "dist\%EXE_NAME%" "%ROOT_EXE%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Built artifact move failed.
  call :print_log
  exit /b 1
)
echo [ Success !! ]

REM Validate the promoted onefile archive before cleanup and launch
echo | set /p="Validating PyInstaller archive..."
call :clear_log
uv run python "tools\verify_pyinstaller_archive.py" "%ROOT_EXE%" --entry "playwright\driver\node.exe" --match-file ".venv\Lib\site-packages\playwright\driver\node.exe" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo PyInstaller archive validation failed.
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

REM Launch the built executable
if "%SKIP_POST_BUILD_RUN%"=="1" (
  echo Skipping post-build launch because WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN=1.
  call :clear_log
  endlocal
  exit /b 0
)

echo | set /p="Running %EXE_NAME%..."
if not exist "%ROOT_EXE%" (
  echo Failure
  echo Failed to start executable.
  echo Executable was not found: %ROOT_EXE%
  exit /b 1
)
start "" "%ROOT_EXE%" > NUL 2>&1
call :wait_for_process_start
if errorlevel 1 (
  echo Failure
  echo Failed to start executable.
  exit /b 1
)
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

:is_process_running
tasklist /FI "IMAGENAME eq %EXE_NAME%" 2> NUL | find /I "%EXE_NAME%" > NUL
if errorlevel 1 exit /b 1
exit /b 0

:wait_for_process_stop
for /L %%I in (1,1,5) do (
  call :is_process_running
  if errorlevel 1 exit /b 0
  call :sleep_one_second
)
call :is_process_running
if errorlevel 1 exit /b 0
exit /b 1

:wait_for_process_start
for /L %%I in (1,1,%POST_BUILD_RUN_START_TIMEOUT_SECONDS%) do (
  call :is_process_running
  if not errorlevel 1 exit /b 0
  call :sleep_one_second
)
call :is_process_running
if not errorlevel 1 exit /b 0
exit /b 1

:sleep_one_second
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 1" > NUL 2>&1
exit /b %ERRORLEVEL%
