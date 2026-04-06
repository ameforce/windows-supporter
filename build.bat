@ECHO OFF
setlocal EnableExtensions DisableDelayedExpansion

REM Variables
set "EXE_BASE=windows-supporter"
set "EXE_NAME=%EXE_BASE%.exe"
set "MAIN_SOURCE=main.py"
set "CURRENT_DIR=%~dp0"
set "ROOT_EXE=%CURRENT_DIR%%EXE_NAME%"
set "STEP_LOG=%TEMP%\%EXE_BASE%-build-%RANDOM%%RANDOM%.log"

REM Switch to repo root
cd /d "%CURRENT_DIR%"
if errorlevel 1 (
  echo Failed to change working directory to "%CURRENT_DIR%"
  exit /b 1
)

REM Stop the running executable before rebuilding
echo | set /p="Shutting down the running %EXE_NAME% process..."
call :clear_log
taskkill /f /im "%EXE_NAME%" > "%STEP_LOG%" 2>&1
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

REM Build the executable
echo | set /p="Building %MAIN_SOURCE% to %EXE_NAME%..."
call :clear_log
uv run python -m PyInstaller -n "%EXE_BASE%" --onefile --noconsole --icon "src\utils\windows_supporter.ico" --collect-all playwright --add-data "src\utils\windows_supporter.ico;src\utils" "%MAIN_SOURCE%" > "%STEP_LOG%" 2>&1
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

REM Remove build byproducts
echo | set /p="Remove build byproducts..."
call :clear_log
if exist "%EXE_BASE%.spec" (
  call "tools\remove_path_with_retry.bat" "%EXE_BASE%.spec" 5 1 > "%STEP_LOG%" 2>&1
  if exist "%EXE_BASE%.spec" (
    echo Failure
    echo Failed to remove generated spec file.
    call :print_log
    exit /b 1
  )
)
if exist "build" (
  call "tools\remove_path_with_retry.bat" "build" 5 1 >> "%STEP_LOG%" 2>&1
  if exist "build" (
    echo Failure
    echo Failed to remove build directory.
    call :print_log
    exit /b 1
  )
)
if exist "dist" (
  call "tools\remove_path_with_retry.bat" "dist" 5 1 >> "%STEP_LOG%" 2>&1
  if exist "dist" (
    echo Failure
    echo Failed to remove dist directory.
    call :print_log
    exit /b 1
  )
)
call :clear_log
echo [ Success !! ]

REM Launch the built executable
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
  timeout /t 1 /nobreak > NUL
)
call :is_process_running
if errorlevel 1 exit /b 0
exit /b 1

:wait_for_process_start
for /L %%I in (1,1,5) do (
  call :is_process_running
  if not errorlevel 1 exit /b 0
  timeout /t 1 /nobreak > NUL
)
call :is_process_running
if not errorlevel 1 exit /b 0
exit /b 1
