@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Compila Smart File Organizer en Windows con PyInstaller.
rem Uso:
rem   build.bat              instala dependencias y compila onedir + onefile
rem   build.bat --skip-install   omite pip install
rem Resultado:
rem   dist\SmartFileOrganizer\SmartFileOrganizer.exe   (onedir, recomendado para probar)
rem   dist\SmartFileOrganizer.exe                      (onefile, para doble clic)

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

echo.
echo === Smart File Organizer - compilacion con PyInstaller ===
echo Python: %PYTHON%
%PYTHON% --version
if errorlevel 1 goto :no_python

if /I "%~1"=="--skip-install" goto :build
echo.
echo === Instalando dependencias de compilacion ===
%PYTHON% -m pip install --upgrade pip
if errorlevel 1 goto :error
%PYTHON% -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

:build
echo.
echo === Generando icono ===
%PYTHON% tools\make_icon.py
if errorlevel 1 (
    echo AVISO: no se pudo generar assets\app.ico. Se compilara sin icono personalizado.
)

echo.
echo === 1/2 Compilando version onedir ===
%PYTHON% -m PyInstaller --noconfirm --clean SmartFileOrganizer-onedir.spec
if errorlevel 1 goto :error

echo.
echo === 2/2 Compilando version onefile ===
%PYTHON% -m PyInstaller --noconfirm --clean SmartFileOrganizer.spec
if errorlevel 1 goto :error

echo.
echo === Compilacion completada ===
echo   onedir:  %CD%\dist\SmartFileOrganizer\SmartFileOrganizer.exe
echo   onefile: %CD%\dist\SmartFileOrganizer.exe
echo.
echo Abre el .exe con doble clic. La configuracion y el historial se guardan en:
echo   %%APPDATA%%\SmartFileOrganizer
echo.
pause
exit /b 0

:no_python
echo.
echo ERROR: no se encontro Python. Instala Python 3.10 o superior y marca
echo "Add Python to PATH", o crea un entorno .venv en esta carpeta.
pause
exit /b 1

:error
echo.
echo ERROR: la compilacion fallo. Revisa el mensaje anterior.
echo Consejo: ejecuta "python -m PyInstaller --clean --noconfirm SmartFileOrganizer.spec"
echo para ver el error completo.
pause
exit /b 1
