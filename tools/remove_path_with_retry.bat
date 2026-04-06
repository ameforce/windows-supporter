@ECHO OFF
setlocal EnableExtensions DisableDelayedExpansion

set "TARGET_PATH=%~1"
set "MAX_ATTEMPTS=%~2"
set "WAIT_SECONDS=%~3"

if not defined TARGET_PATH exit /b 1
if not defined MAX_ATTEMPTS set "MAX_ATTEMPTS=5"
if not defined WAIT_SECONDS set "WAIT_SECONDS=1"
set /a WAIT_TICKS=%WAIT_SECONDS% + 1

if not exist "%TARGET_PATH%" exit /b 0

for /L %%I in (1,1,%MAX_ATTEMPTS%) do (
  if exist "%TARGET_PATH%\" (
    rmdir /S /Q "%TARGET_PATH%" > NUL 2>&1
  ) else (
    del /F /Q "%TARGET_PATH%" > NUL 2>&1
  )

  if not exist "%TARGET_PATH%" exit /b 0

  if %%I LSS %MAX_ATTEMPTS% ping 127.0.0.1 -n %WAIT_TICKS% > NUL
)

if not exist "%TARGET_PATH%" exit /b 0
exit /b 1
