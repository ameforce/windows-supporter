@ECHO OFF
setlocal EnableExtensions DisableDelayedExpansion

REM Variables
set "EXE_BASE=windows-supporter"
set "EXE_NAME=%EXE_BASE%.exe"
set "MAIN_SOURCE=main.py"
set "CURRENT_DIR=%~dp0"
set "ROOT_EXE=%CURRENT_DIR%%EXE_NAME%"
set "ROOT_EXE_BACKUP=%CURRENT_DIR%%EXE_BASE%.previous.exe"
set "ROOT_PROMOTION_TRANSACTION=%CURRENT_DIR%%EXE_BASE%.promotion-pending"
set "ROOT_BACKUP_CREATED=0"
set "BUILD_GENERATED_DIR=build\generated"
set "BUILD_INFO_MODULE=%BUILD_GENERATED_DIR%\windows_supporter_build_info.py"
set "VERSION_FILE=%BUILD_GENERATED_DIR%\%EXE_BASE%-version-info.txt"
set "STEP_LOG=%TEMP%\%EXE_BASE%-build-%RANDOM%%RANDOM%.log"
set "POST_BUILD_RUN_START_TIMEOUT_SECONDS=60"
set "SKIP_POST_BUILD_RUN=0"
if /I "%WINDOWS_SUPPORTER_SKIP_POST_BUILD_RUN%"=="1" set "SKIP_POST_BUILD_RUN=1"
set "ARTIFACT_ONLY=0"
if /I "%WINDOWS_SUPPORTER_BUILD_ARTIFACT_ONLY%"=="1" (
  set "ARTIFACT_ONLY=1"
  set "SKIP_POST_BUILD_RUN=1"
)
set "EMIT_STEP_LOG=0"
if /I "%WINDOWS_SUPPORTER_EMIT_STEP_LOG%"=="1" set "EMIT_STEP_LOG=1"
if "%EMIT_STEP_LOG%"=="1" echo WINDOWS_SUPPORTER_STEP_LOG=%STEP_LOG%

REM Switch to repo root
cd /d "%CURRENT_DIR%"
if errorlevel 1 (
  echo Failed to change working directory to "%CURRENT_DIR%"
  exit /b 1
)

REM Stop the running executable before rebuilding
if "%ARTIFACT_ONLY%"=="0" (
  echo | set /p="Shutting down the running %EXE_NAME% process..."
  call :clear_log
  taskkill /f /t /im "%EXE_NAME%" > "%STEP_LOG%" 2>&1
  if errorlevel 129 (
    echo Failure
    echo Failed to stop the running %EXE_NAME% process.
    call :print_log
    exit /b 1
  ) else if errorlevel 128 (
    echo [ Not running ]
  ) else if errorlevel 1 (
    echo Failure
    echo Failed to stop the running %EXE_NAME% process.
    call :print_log
    exit /b 1
  ) else (
    echo [ Success !! ]
  )
  call :wait_for_process_stop
  if errorlevel 1 (
    echo Failure
    echo %EXE_NAME% is still running after taskkill.
    exit /b 1
  )
  echo | set /p="Recovering an interrupted executable promotion..."
  call :clear_log
  call :recover_interrupted_promotion
  if errorlevel 1 (
    echo Failure
    echo Failed to recover the interrupted executable promotion.
    call :print_log
    exit /b 1
  )
  echo [ Success !! ]
)
if "%ARTIFACT_ONLY%"=="1" (
  echo Artifact-only mode: skipping shutdown of the installed %EXE_NAME% process.
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
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python -m PyInstaller -n "%EXE_BASE%" --onefile --noconsole --icon "src\utils\windows_supporter.ico" --version-file "%VERSION_FILE%" --paths "%BUILD_GENERATED_DIR%" --hidden-import windows_supporter_build_info --collect-all playwright --add-data "src\utils\windows_supporter.ico;src\utils" "%MAIN_SOURCE%" > "%STEP_LOG%" 2>&1
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
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python "tools\verify_pyinstaller_archive.py" "dist\%EXE_NAME%" --entry "playwright\driver\node.exe" --match-file ".venv\Lib\site-packages\playwright\driver\node.exe" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo PyInstaller archive validation failed.
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

REM Preserve the last known executable until the promoted path passes its worker smoke
echo | set /p="Backing up the current %EXE_NAME%..."
call :clear_log
call :backup_root_executable
if errorlevel 1 (
  echo Failure
  echo Failed to preserve the current %EXE_NAME% before promotion.
  call :print_log
  exit /b 1
)
if "%ROOT_BACKUP_CREATED%"=="1" (
  echo [ Success !! ]
) else (
  echo [ Not present ]
)

REM Promote the verified artifact to the repo root
echo | set /p="Moving %EXE_NAME%..."
call :clear_log
move /Y "dist\%EXE_NAME%" "%ROOT_EXE%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Built artifact move failed.
  call :print_log
  call :restore_root_executable
  exit /b 1
)
echo [ Success !! ]

REM Re-check the frozen child-process boundary from the permanent execution path.
REM Smart App Control can make a different decision after the artifact is promoted.
echo | set /p="Validating promoted worker boundary..."
call :clear_log
"%WINDOWS_SUPPORTER_UV_EXE%" run --locked python "tools\verify_codex_usage_worker_smoke.py" "%ROOT_EXE%" > "%STEP_LOG%" 2>&1
if errorlevel 1 (
  echo Failure
  echo Promoted worker boundary validation failed. Restoring the previous executable.
  call :print_log
  call :restore_root_executable
  if errorlevel 1 (
    echo Failed to restore the previous %EXE_NAME%.
    call :print_log
  )
  exit /b 1
)
echo [ Success !! ]
call :discard_root_executable_backup
if errorlevel 1 (
  echo Failure
  echo Failed to remove the previous executable backup.
  call :print_log
  exit /b 1
)

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

:backup_root_executable
set "ROOT_BACKUP_CREATED=0"
if exist "%ROOT_EXE_BACKUP%" del /F /Q "%ROOT_EXE_BACKUP%" > "%STEP_LOG%" 2>&1
if exist "%ROOT_EXE_BACKUP%" exit /b 1
if not exist "%ROOT_EXE%" exit /b 0
mklink /H "%ROOT_EXE_BACKUP%" "%ROOT_EXE%" > "%STEP_LOG%" 2>&1
if not exist "%ROOT_EXE_BACKUP%" (
  move /Y "%ROOT_EXE%" "%ROOT_EXE_BACKUP%" >> "%STEP_LOG%" 2>&1
  if errorlevel 1 exit /b 1
)
if not exist "%ROOT_EXE_BACKUP%" exit /b 1
set "ROOT_BACKUP_CREATED=1"
> "%ROOT_PROMOTION_TRANSACTION%" echo pending
if not exist "%ROOT_PROMOTION_TRANSACTION%" (
  call :restore_root_executable
  exit /b 1
)
exit /b 0

:restore_root_executable
call :clear_log
taskkill /f /t /im "%EXE_NAME%" > "%STEP_LOG%" 2>&1
call :wait_for_process_stop
if errorlevel 1 exit /b 1
if exist "%ROOT_EXE%" del /F /Q "%ROOT_EXE%" >> "%STEP_LOG%" 2>&1
if exist "%ROOT_EXE%" exit /b 1
if "%ROOT_BACKUP_CREATED%"=="1" (
  move /Y "%ROOT_EXE_BACKUP%" "%ROOT_EXE%" >> "%STEP_LOG%" 2>&1
  if errorlevel 1 exit /b 1
  if not exist "%ROOT_EXE%" exit /b 1
)
if exist "%ROOT_PROMOTION_TRANSACTION%" del /F /Q "%ROOT_PROMOTION_TRANSACTION%" >> "%STEP_LOG%" 2>&1
if exist "%ROOT_PROMOTION_TRANSACTION%" exit /b 1
set "ROOT_BACKUP_CREATED=0"
exit /b 0

:discard_root_executable_backup
call :clear_log
if exist "%ROOT_PROMOTION_TRANSACTION%" del /F /Q "%ROOT_PROMOTION_TRANSACTION%" > "%STEP_LOG%" 2>&1
if exist "%ROOT_PROMOTION_TRANSACTION%" exit /b 1
if exist "%ROOT_EXE_BACKUP%" del /F /Q "%ROOT_EXE_BACKUP%" > "%STEP_LOG%" 2>&1
if exist "%ROOT_EXE_BACKUP%" exit /b 1
set "ROOT_BACKUP_CREATED=0"
exit /b 0

:recover_interrupted_promotion
if not exist "%ROOT_PROMOTION_TRANSACTION%" exit /b 0
if not exist "%ROOT_EXE_BACKUP%" (
  > "%STEP_LOG%" echo Promotion transaction exists without a rollback executable.
  exit /b 1
)
set "ROOT_BACKUP_CREATED=1"
call :restore_root_executable
exit /b %ERRORLEVEL%

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
"%SystemRoot%\System32\ping.exe" 127.0.0.1 -n 2 -w 1000 > NUL 2>&1
exit /b %ERRORLEVEL%
