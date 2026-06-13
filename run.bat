@echo off
setlocal

:: -----------------------------------------------------------
:: Project root  (handles spaces in folder name)
:: -----------------------------------------------------------
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%venv\Scripts\python.exe"
set "VENV_PIP=%ROOT%venv\Scripts\pip.exe"

:: -----------------------------------------------------------
:: VPS / SSH Tunnel settings
:: -----------------------------------------------------------
set "VPS_HOST=1.201.123.110"
set "VPS_USER=ubuntu"
set "SSH_KEY=%ROOT%ssh_keys\SSH_KeyPair-260527213658.pem"
set "SOCKS5_PORT=1080"
set "SSH_PID="

echo.
echo ============================================
echo  Naver-to-Coupang Pipeline  /  Boot Check
echo ============================================
echo.

:: -----------------------------------------------------------
:: [1/5] venv
:: -----------------------------------------------------------
echo [1/5] venv check ...
if exist "%VENV_PY%" goto :venv_ok

echo [1/5] venv not found -- creating...
python -m venv "%ROOT%venv"
if errorlevel 1 goto :err_venv
echo [1/5] venv created.

:venv_ok
echo [1/5] venv OK

:: -----------------------------------------------------------
:: [2/5] Packages
:: -----------------------------------------------------------
echo [2/5] Syncing packages from requirements.txt ...
"%VENV_PIP%" install -q -r "%ROOT%requirements.txt"
if errorlevel 1 (
    echo [2/5] Retrying without -q ...
    "%VENV_PIP%" install -r "%ROOT%requirements.txt"
    if errorlevel 1 goto :err_pkg
)
echo [2/5] Packages OK

:: -----------------------------------------------------------
:: [3/5] Playwright browser
:: -----------------------------------------------------------
echo [3/5] Playwright Chromium check ...
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 echo [3/5] WARNING: Playwright install returned error -- continuing
echo [3/5] Browser OK

:: -----------------------------------------------------------
:: [4/5] SSH SOCKS5 tunnel
::
::   !! IMPORTANT: Labels MUST be outside if/else parentheses !!
::   All branching is done with "goto" at the top level.
:: -----------------------------------------------------------
echo [4/5] SSH SOCKS5 tunnel check (port %SOCKS5_PORT%) ...

netstat -ano | findstr ":%SOCKS5_PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel% == 0 goto :tunnel_already_up

:: Tunnel is not running -- start it
echo [4/5] Starting SSH tunnel to %VPS_HOST% ...

:: Check ssh.exe is available
where ssh >nul 2>&1
if errorlevel 1 (
    echo [4/5] WARNING: ssh.exe not found in PATH. Skipping tunnel.
    echo        Install OpenSSH: Settings ^> Apps ^> Optional Features ^> OpenSSH Client
    goto :no_tunnel
)

:: Check key file exists
if not exist "%SSH_KEY%" (
    echo [4/5] WARNING: SSH key not found: %SSH_KEY%
    echo        Coupang API will use direct connection.
    goto :no_tunnel
)

:: Restrict key permissions so OpenSSH accepts it
icacls "%SSH_KEY%" /inheritance:r /grant:r "%USERNAME%:(R)" >nul 2>&1

:: Launch tunnel in a minimised background window
start "SSH-SOCKS5" /min ssh -N -D %SOCKS5_PORT% -i "%SSH_KEY%" -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes %VPS_USER%@%VPS_HOST%

:: Wait up to 10 s for the port to appear
echo [4/5] Waiting for tunnel (up to 10 s) ...
set /a WAIT=0

:tunnel_wait
timeout /t 1 /nobreak >nul
set /a WAIT+=1
netstat -ano | findstr ":%SOCKS5_PORT% " | findstr "LISTENING" >nul 2>&1
if %errorlevel% == 0 goto :tunnel_found
if %WAIT% LSS 10 goto :tunnel_wait

echo [4/5] WARNING: Tunnel did not come up in 10 s. Continuing without proxy.
goto :no_tunnel

:tunnel_already_up
echo [4/5] SOCKS5 already running on port %SOCKS5_PORT% -- skip
goto :no_tunnel

:tunnel_found
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%SOCKS5_PORT% " ^| findstr "LISTENING"') do set "SSH_PID=%%p"
echo [4/5] Tunnel UP on port %SOCKS5_PORT%  ^(PID: %SSH_PID%^)

:no_tunnel

:: -----------------------------------------------------------
:: [5/5] Launch GUI
:: -----------------------------------------------------------
echo [5/5] Launching GUI ...
echo.
"%VENV_PY%" "%ROOT%app_gui.py"
if errorlevel 1 (
    echo.
    echo ERROR: GUI exited with an error -- see log above.
    pause
)
goto :done

:: -----------------------------------------------------------
:: Error handlers
:: -----------------------------------------------------------
:err_venv
echo.
echo ============================================
echo  ERROR [1/5]: Cannot create virtual env
echo  Python 3.10+ must be installed and in PATH
echo ============================================
pause
exit /b 1

:err_pkg
echo.
echo ============================================
echo  ERROR [2/5]: Package installation failed
echo  Check internet connection and retry
echo ============================================
pause
exit /b 1

:: -----------------------------------------------------------
:: Cleanup: kill SSH tunnel (3-tier fallback)
:: -----------------------------------------------------------
:done
echo.
echo [Cleanup] Terminating SSH SOCKS5 tunnel ...

:: A) PID direct kill (most reliable -- captured when tunnel came up)
if defined SSH_PID (
    taskkill /pid %SSH_PID% /f >nul 2>&1
    echo [Cleanup] PID %SSH_PID% terminated.
    goto :cleanup_done
)

:: B) Kill by window title (fallback when PID was not captured)
taskkill /f /fi "WINDOWTITLE eq SSH-SOCKS5" >nul 2>&1

:: C) Scan port 1080 for ssh.exe and kill (last resort)
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":%SOCKS5_PORT% " ^| findstr "LISTENING"') do (
    tasklist /fi "PID eq %%p" /fi "IMAGENAME eq ssh.exe" 2>nul | findstr /i "ssh.exe" >nul
    if not errorlevel 1 (
        taskkill /pid %%p /f >nul 2>&1
        echo [Cleanup] ssh.exe PID %%p terminated.
    )
)

:cleanup_done
echo [Cleanup] Done.
endlocal
pause
