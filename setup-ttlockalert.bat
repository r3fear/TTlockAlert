@echo off
color 07
title TTLock Alert - Panel de Control
cd /d "%~dp0"
set "SERVICE=TTLockAlert"

:: ================================================================
:MENU
cls
echo.
echo  ============================================================
echo     TTLock Alert  -  Panel de Control
echo  ============================================================
echo.
echo   TOKEN TTLOCK
echo   ------------------------------------------------------------
echo    [1] Verificar token             estado del cache OAuth2
echo.
echo   SERVICIO DE WINDOWS
echo   ------------------------------------------------------------
echo    [2] Instalar servicio           dependencias Python + NSSM
echo    [3] Iniciar servicio
echo    [4] Detener servicio
echo    [5] Reiniciar servicio
echo.
echo   DIAGNOSTICO
echo   ------------------------------------------------------------
echo    [6] Ver log en tiempo real
echo    [7] Consultar cerraduras        lockId(s) desde TTLock API
echo    [8] Verificar Vercel Relay      estado y cola de eventos
echo    [9] Enviar evento de prueba     simular apertura y verificar flujo
echo.
echo   ------------------------------------------------------------
echo    [0] Salir
echo  ============================================================
echo.
set "OPCION="
set /p OPCION="   Selecciona una opcion [0-9]: "

if "%OPCION%"=="1" goto VER_TOKEN
if "%OPCION%"=="2" goto INSTALAR_SERVICIO
if "%OPCION%"=="3" goto INICIAR
if "%OPCION%"=="4" goto DETENER
if "%OPCION%"=="5" goto REINICIAR
if "%OPCION%"=="6" goto VER_LOG
if "%OPCION%"=="7" goto LISTAR_CERRADURAS
if "%OPCION%"=="8" goto CHECK_VERCEL
if "%OPCION%"=="9" goto TEST_WEBHOOK
if "%OPCION%"=="0" goto SALIR

echo.
echo   Opcion invalida. Intenta de nuevo.
timeout /t 2 /nobreak >nul
goto MENU

:: ================================================================
:VER_TOKEN
cls
echo.
echo  ============================================================
echo     Estado del token TTLock OAuth2
echo  ============================================================
echo.

:: Leer token_file desde config.yaml con Python
set "TOKEN_FILE="
for /f "usebackq tokens=* delims=" %%i in (`py -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c['ttlock'].get('token_file','ttlock_token.cache'))" 2^>nul`) do set "TOKEN_FILE=%%i"
if not defined TOKEN_FILE set "TOKEN_FILE=ttlock_token.cache"

echo   Archivo: %TOKEN_FILE%
echo.

if not exist "%TOKEN_FILE%" (
    echo   [?] Token no encontrado.
    echo.
    echo   El token se obtiene automaticamente la primera vez que
    echo   el servicio inicia. No es necesario generarlo manualmente.
    goto TOKEN_FIN
)

echo   [OK] Token encontrado.
for %%q in ("%TOKEN_FILE%") do echo   Ultima modificacion: %%~tq
echo.
py -c "import json,time; d=json.load(open('%TOKEN_FILE%')); exp=d.get('expires_at',0); rem=exp-time.time(); dias=int(rem//86400); h=int((rem%%86400)//3600); print(f'  Expira en: {dias} dias, {h}h') if rem>0 else print('  [!] TOKEN EXPIRADO — el servicio lo renovara al iniciar.')"

:TOKEN_FIN
echo.
pause
goto MENU

:: ================================================================
:INSTALAR_SERVICIO
cls
echo.
echo  ============================================================
echo     Instalar TTLock Alert como servicio de Windows (NSSM)
echo  ============================================================
echo.
echo   Verificando requisitos...
echo.

:: 1. Permisos de Administrador
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] Se requieren permisos de Administrador.
    echo       Cierra este bat y ejecutalo con clic derecho:
    echo       "Ejecutar como administrador".
    goto FIN_INSTALAR
)
echo   [OK] Permisos de Administrador

:: 2. Python
set "PYTHON_EXE="
for /f "tokens=* delims=" %%i in ('py -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%i"
if not defined PYTHON_EXE (
    echo   [!] Python no encontrado en PATH.
    echo       Instala Python 3.11+ desde https://python.org
    goto FIN_INSTALAR
)
echo   [OK] Python: %PYTHON_EXE%

:: 3. NSSM
set "NSSM_EXE="
for /f "tokens=* delims=" %%i in ('where nssm 2^>nul') do if not defined NSSM_EXE set "NSSM_EXE=%%i"
if not defined NSSM_EXE (
    echo   [!] NSSM no encontrado en PATH.
    echo       Descarga nssm.exe desde: https://nssm.cc/download
    echo       Copia nssm.exe a: C:\Windows\System32\
    goto FIN_INSTALAR
)
echo   [OK] NSSM: %NSSM_EXE%

:: 4. config.yaml
if not exist "%~dp0config.yaml" (
    echo   [!] config.yaml no encontrado.
    echo       Ejecuta: copy config.yaml.example config.yaml
    echo       Luego edita config.yaml con tus credenciales.
    goto FIN_INSTALAR
)
echo   [OK] config.yaml

:: 5. Dependencias Python (PyYAML minimo)
py -c "import yaml" >nul 2>&1
if %errorlevel% neq 0 (
    echo   [?] PyYAML no instalado. Se instalara con requirements.txt...
) else (
    echo   [OK] PyYAML disponible
)

:: 6. wa-gateway (informativo — no bloquea la instalacion)
set "WA_URL="
for /f "usebackq tokens=* delims=" %%i in (`py -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c.get('whatsapp',{}).get('gateway_url',''))" 2^>nul`) do set "WA_URL=%%i"
if not defined WA_URL (
    echo   [?] whatsapp.gateway_url no definido en config.yaml — omitiendo verificacion.
    goto CHECK_WA_FIN
)
curl.exe -s --max-time 3 "%WA_URL%/status" >nul 2>&1
if %errorlevel% neq 0 (
    echo   [?] wa-gateway no responde en %WA_URL%
    echo       El servicio se instala igual; las alertas usaran email fallback
    echo       hasta que wa-gateway este disponible.
) else (
    echo   [OK] wa-gateway disponible en %WA_URL%
)
:CHECK_WA_FIN

:: 7. Servicio ya instalado?
nssm status %SERVICE% >nul 2>&1
if %errorlevel% equ 0 (
    echo.
    echo   [!] El servicio "%SERVICE%" ya esta instalado.
    set "REINSTALAR="
    set /p REINSTALAR="   Deseas reinstalarlo? [s/n]: "
    if /i not "%REINSTALAR%"=="s" goto FIN_INSTALAR
    echo.
    echo   Removiendo servicio existente...
    nssm stop %SERVICE% >nul 2>&1
    nssm remove %SERVICE% confirm >nul 2>&1
)

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

echo.
echo   Todas las verificaciones completadas.
echo   Procediendo con la instalacion...
echo.
pause

:: 8. pip install
echo.
echo   Instalando dependencias Python (pip install -r requirements.txt)...
py -m pip install -r "%APP_DIR%\requirements.txt"
if %errorlevel% neq 0 (
    echo   [!] Error en pip install. Revisa la conexion a internet o los permisos.
    goto FIN_INSTALAR
)
echo   [OK] Dependencias instaladas.

:: 9. Crear carpeta logs
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"
echo   [OK] Carpeta logs\ creada.

:: 10. Instalar y configurar servicio NSSM
echo.
echo   Instalando servicio NSSM...
nssm install %SERVICE% "%PYTHON_EXE%"
nssm set %SERVICE% AppParameters "%APP_DIR%\main.py"
nssm set %SERVICE% AppDirectory "%APP_DIR%"
nssm set %SERVICE% AppStdout "%APP_DIR%\logs\ttlock-alert.log"
nssm set %SERVICE% AppStderr "%APP_DIR%\logs\ttlock-alert.log"
nssm set %SERVICE% AppRotateFiles 1
nssm set %SERVICE% Start SERVICE_AUTO_START
echo   [OK] Servicio configurado.

echo.
echo   Iniciando servicio...
nssm start %SERVICE%
if %errorlevel% neq 0 (
    echo.
    echo   [!] El servicio se instalo pero no pudo iniciarse.
    echo       Revisa el log: %APP_DIR%\logs\ttlock-alert.log
    echo       Asegurate de que config.yaml este completo.
) else (
    echo   [OK] Servicio iniciado.
    echo.
    echo  ============================================================
    echo     Instalacion completada.
    echo     Servicio:  %SERVICE%
    echo     Log:       %APP_DIR%\logs\ttlock-alert.log
    echo.
    echo     Ver log en tiempo real (PowerShell):
    echo     Get-Content "%APP_DIR%\logs\ttlock-alert.log" -Wait -Tail 50
    echo  ============================================================
)

:FIN_INSTALAR
echo.
pause
goto MENU

:: ================================================================
:INICIAR
cls
echo.
echo   Iniciando servicio %SERVICE%...
echo.
where nssm >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] NSSM no encontrado. Instala el servicio primero (opcion 2).
    pause
    goto MENU
)
nssm start %SERVICE%
echo.
pause
goto MENU

:: ================================================================
:DETENER
cls
echo.
echo   Deteniendo servicio %SERVICE%...
echo.
where nssm >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] NSSM no encontrado.
    echo       Para detener manualmente: Stop-Process -Name python -Force
    pause
    goto MENU
)
nssm stop %SERVICE%
echo.
pause
goto MENU

:: ================================================================
:REINICIAR
cls
echo.
echo   Reiniciando servicio %SERVICE%...
echo.
where nssm >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] NSSM no encontrado.
    pause
    goto MENU
)
nssm restart %SERVICE%
echo.
pause
goto MENU

:: ================================================================
:VER_LOG
cls
echo.
echo  ============================================================
echo     Log en tiempo real  --  Ctrl+C para salir
echo  ============================================================
echo.
set "LOG_PATH="
for /f "usebackq tokens=* delims=" %%i in (`py -c "import os; print(os.path.join(os.getcwd(),'logs','ttlock-alert.log'))" 2^>nul`) do set "LOG_PATH=%%i"
if not defined LOG_PATH set "LOG_PATH=%~dp0logs\ttlock-alert.log"

if not exist "%LOG_PATH%" (
    echo   [!] El archivo de log no existe todavia.
    echo       Inicia el servicio primero (opcion 3).
    echo.
    pause
    goto MENU
)
powershell -NoProfile -Command "Get-Content '%LOG_PATH%' -Wait -Tail 50"
goto MENU

:: ================================================================
:LISTAR_CERRADURAS
cls
echo.
echo  ============================================================
echo     Cerraduras vinculadas a la cuenta TTLock
echo  ============================================================
echo.
if not exist "%~dp0config.yaml" (
    echo   [!] config.yaml no encontrado.
    pause
    goto MENU
)
py ttlock_tools.py --list-locks
echo.
pause
goto MENU

:: ================================================================
:CHECK_VERCEL
cls
echo.
echo  ============================================================
echo     Verificar Vercel Relay
echo  ============================================================
echo.
if not exist "%~dp0config.yaml" (
    echo   [!] config.yaml no encontrado.
    pause
    goto MENU
)
py ttlock_tools.py --check-vercel
echo.
pause
goto MENU

:: ================================================================
:TEST_WEBHOOK
cls
echo.
echo  ============================================================
echo     Enviar evento de prueba al Vercel Relay
echo  ============================================================
echo.
echo   Simula una apertura por App (recordType 1) y verifica
echo   que el evento llega correctamente a Upstash Redis.
echo.
echo   Nota: si TTLock Alert esta corriendo como servicio,
echo   puede consumir el evento antes de que lo verifiquemos
echo   (lo cual tambien indica que el flujo funciona).
echo.
if not exist "%~dp0config.yaml" (
    echo   [!] config.yaml no encontrado.
    pause
    goto MENU
)
py ttlock_tools.py --test-webhook
echo.
pause
goto MENU

:: ================================================================
:SALIR
exit /b 0
