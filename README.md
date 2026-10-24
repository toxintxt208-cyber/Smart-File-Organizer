# Smart File Organizer

Aplicación de escritorio para Windows que organiza de forma segura los archivos
del **primer nivel** de Descargas, Escritorio o cualquier carpeta personalizada.
Clasifica por extensión, mueve cada archivo a una subcarpeta dentro del mismo
origen, guarda un historial restaurable y permite simular los cambios antes de
tocar nada.

Incluye interfaz gráfica con Tkinter, línea de comandos, configuración JSON
persistente, historial con deshacer, modo automático opcional y vigilancia de
archivos nuevos con `watchdog` (dependencia opcional).

> **Principio de seguridad:** la aplicación nunca sobrescribe, nunca elimina,
> nunca ejecuta archivos y nunca solicita permisos de administrador.

---

## English summary

**Smart File Organizer** is a Windows desktop app (Python 3.11+) that safely
sorts top-level files into category folders based on their extensions. It ships
with a Tkinter GUI, a CLI, a JSON configuration, duplicate-safe moves, dry-run
previews, persistent history with undo, an optional scheduler and optional
real-time folder watching via `watchdog`.

Files are never overwritten or deleted, symlinks and non-regular files are
ignored, and every move stays inside its configured source root. The GUI asks
for confirmation before organizing. The project includes 121 automated tests
that use temporary directories only. See [Quick start](#quick-start) for the
English commands.

---

## Características

- **Clasificación configurable** por extensión, sin distinguir mayúsculas y con
  soporte para extensiones compuestas como `.tar.gz`.
- **Análisis previo** y **previsualización** sin crear carpetas ni mover nada.
- **Movimientos seguros**: reserva exclusiva del nombre de destino, renombrado
  ante colisiones (`informe_1.pdf`), comprobación de rutas permitidas y
  restauración del origen si algo falla.
- **Historial persistente** con identificadores únicos, estadísticas y
  restauración del último movimiento.
- **Interfaz responsiva**: el trabajo pesado se ejecuta en un hilo aparte y la
  interfaz se actualiza mediante una cola y `after()`.
- **Botón Detener** con parada cooperativa: termina el archivo actual y no
  interrumpe un movimiento a medias.
- **Configuración tolerante a fallos**: validación, normalización, escritura
  atómica y recuperación automática si el JSON está dañado.
- **Detección de rutas reales de Windows**, incluidas las carpetas redirigidas a
  OneDrive, mediante la API de carpetas conocidas y el registro del usuario.
- **Automatización opcional** desactivada por defecto, con intervalo
  configurable y sin tareas programadas del sistema.
- **Vigilancia opcional** de archivos nuevos con espera de estabilización,
  pausa/reanudación y prevención de duplicados.
- **Inicio con Windows opcional** mediante un acceso directo en la carpeta
  Inicio del usuario, con consentimiento explícito. No se modifica el registro
  ni se crean tareas programadas.
- **CLI completa** para analizar, simular, organizar, consultar estadísticas,
  validar la configuración, vigilar, programar y restaurar.
- **Sin dependencias obligatorias** fuera de la biblioteca estándar. Solo
  `watchdog` es opcional.

---

## Requisitos

- Windows 10 u 11 (la interfaz está pensada para Windows; el núcleo y la CLI
  también funcionan en macOS y Linux, salvo el inicio automático).
- Python **3.11 o superior** con Tcl/Tk (incluido en el instalador oficial de
  Windows y en Python de python.org).
- No se necesitan privilegios de administrador.

Comprobación rápida:

```powershell
python --version
python -c "import tkinter; print('tkinter OK')"
```

Desarrollo y pruebas realizados con **Python 3.12.10 en Windows**.

---

## Instalación

```powershell
git clone <URL-del-repositorio>
cd "SmartFileOrganizer"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` instala únicamente la dependencia opcional `watchdog`. Si no
la instalas, la aplicación sigue funcionando en modo manual y programado; solo
se deshabilita la vigilancia en tiempo real.

También puedes ejecutar sin instalar nada:

```powershell
python main.py
```

La primera ejecución crea la configuración en
`%APPDATA%\SmartFileOrganizer\config.json` y detecta Descargas y Escritorio.

---

## Ejecutable de Windows (.exe)

La aplicación conserva la GUI y la CLI. Para generar un ejecutable que se abra
con doble clic, sin consola y sin instalar Python en el equipo de destino:

```powershell
python -m pip install -r requirements-build.txt
python tools\make_icon.py
python -m PyInstaller --noconfirm --clean SmartFileOrganizer-onedir.spec
python -m PyInstaller --noconfirm --clean SmartFileOrganizer.spec
```

O simplemente ejecuta `build.bat` con doble clic. El script instala las
dependencias de compilación, genera el icono y crea las dos variantes.

| Variante | Salida | Uso recomendado |
| --- | --- | --- |
| **onedir** | `dist\SmartFileOrganizer\SmartFileOrganizer.exe` | Probar y distribuir una carpeta completa. Arranca más rápido. |
| **onefile** | `dist\SmartFileOrganizer.exe` | Un solo archivo para copiar y abrir con doble clic. |

Ambas variantes son **sin consola** (`--windowed`). La configuración, el
historial, el bloqueo y los registros **no** se guardan dentro del `.exe` ni en
la carpeta temporal de PyInstaller: siguen en
`%APPDATA%\SmartFileOrganizer\`, por lo que se conservan al cerrar y volver a
abrir. Esto también mantiene compatibles los datos creados con la versión
Python.

> `%APPDATA%` es la ubicación adecuada para configuración e historial de
> usuario y ya era la usada por la aplicación. No se cambió a `%LOCALAPPDATA%`
> para no perder configuraciones e historiales existentes.

### Recursos incluidos

- `SmartFileOrganizer.spec`: configuración onefile.
- `SmartFileOrganizer-onedir.spec`: configuración onedir.
- `version_info.txt`: metadatos del ejecutable.
- `tools/make_icon.py`: genera `assets/app.ico` durante la compilación.
- `requirements-build.txt`: PyInstaller y Pillow (solo compilación).
- `build.bat`: compilación con doble clic.

La aplicación no necesita archivos de solo lectura empaquetados: la
configuración de ejemplo vive en `config/config.example.json` y el `.exe`
genera la suya en `%APPDATA%`. No se empaqueta `config.json` personal.

### Abrir el ejecutable

1. Abre `dist\SmartFileOrganizer.exe` con doble clic.
2. Si Windows SmartScreen muestra un aviso por ser un ejecutable sin firma,
   usa **Más información → Ejecutar de todos modos**.
3. La primera vez se crea `%APPDATA%\SmartFileOrganizer\config.json`.
4. Los errores de arranque quedan en
   `%APPDATA%\SmartFileOrganizer\logs\smart_file_organizer.log`.

La CLI sigue disponible en el código fuente (`python main.py --analyze`). El
`.exe` se compila en modo ventana para no mostrar una consola al hacer doble
clic; si necesitas la CLI con salida visible, usa `python main.py` o compila una
variante con `console=True` en el `.spec`.

---

## Uso rápido

### Interfaz gráfica

```powershell
python main.py
```

Flujo recomendado:

1. Selecciona una carpeta con **Examinar…** o escribe su ruta.
2. Pulsa **Analizar archivos** para ver la clasificación propuesta.
3. Pulsa **Previsualizar cambios** para revisar destinos y conflictos.
4. Pulsa **Organizar ahora** y confirma el diálogo.
5. Si algo no salió como esperabas, usa **Restaurar último movimiento**.
6. Consulta **Ver historial** para ver operaciones anteriores y su detalle.

Botones principales:

| Botón | Acción |
| --- | --- |
| **Analizar archivos** | Lee el primer nivel y calcula categorías y destinos. No modifica nada. |
| **Previsualizar cambios** | Abre una ventana con el plan detallado antes de mover. |
| **Organizar ahora** | Analiza, pide confirmación y ejecuta los movimientos. |
| **Detener proceso** | Solicita una parada segura tras el archivo actual. |
| **Abrir carpeta de destino** | Abre la carpeta del archivo o categoría seleccionada. |
| **Ver historial** | Muestra operaciones, estados y detalle de archivos. |
| **Restaurar último movimiento** | Deshace la última organización que aún sea restaurable. |

La interfaz también permite editar categorías y extensiones, activar la
organización automática, configurar la vigilancia, ajustar la espera de
estabilización y activar o desactivar el inicio con Windows.

### Línea de comandos

Sin argumentos se abre la interfaz gráfica. Los modos son mutuamente
excluyentes.

```powershell
# Analizar los orígenes configurados
python main.py --analyze

# Analizar una carpeta concreta
python main.py --source "C:\Users\TuUsuario\Downloads" --analyze

# Simular sin mover ni crear carpetas
python main.py --dry-run

# Organizar de verdad
python main.py --organize

# Estadísticas del historial
python main.py --stats

# Validar y mostrar la configuración
python main.py --validate-config

# Restaurar el último movimiento (pide confirmación)
python main.py --undo

# Restaurar sin preguntar, renombrando si el nombre original está ocupado
python main.py --undo --yes --collision rename

# Vigilancia en primer plano (requiere watchdog)
python main.py --watch

# Organización periódica en primer plano (debe estar habilitada)
python main.py --schedule

# Inicio con Windows (crea un acceso directo en la carpeta Inicio)
python main.py --install-startup --yes
python main.py --remove-startup --yes
```

Opciones útiles:

- `--config RUTA`: usa otro archivo de configuración. El historial, el bloqueo y
  los registros se guardan en la misma carpeta que ese archivo.
- `--source RUTA`: puede repetirse. Si se omite, se usan los orígenes
  habilitados en la configuración.
- `--collision ask|skip|rename`: solo con `--undo`. `ask` es el valor por
  defecto; sin terminal interactiva se omiten los conflictos.
- `--yes`: confirma operaciones que normalmente piden interacción. No evita la
  validación de seguridad.
- `--autostart`: uso interno del acceso directo de Windows.

Códigos de salida:

| Código | Significado |
| --- | --- |
| `0` | Operación completada correctamente. |
| `1` | Error controlado o incidencias durante la operación. |
| `2` | Uso incorrecto, ruta inválida o automatización desactivada. |
| `3` | Falta una dependencia opcional, por ejemplo `watchdog`. |
| `130` | Operación cancelada por el usuario. |

---

## Clasificación por defecto

| Categoría | Extensiones |
| --- | --- |
| `Documentos` | `.pdf`, `.doc`, `.docx`, `.odt`, `.txt`, `.rtf` |
| `Hojas_de_calculo` | `.xls`, `.xlsx`, `.csv`, `.ods` |
| `Presentaciones` | `.ppt`, `.pptx`, `.odp` |
| `Imagenes` | `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`, `.svg` |
| `Videos` | `.mp4`, `.mkv`, `.mov`, `.avi`, `.webm` |
| `Audio` | `.mp3`, `.wav`, `.flac`, `.aac`, `.ogg` |
| `Comprimidos` | `.zip`, `.rar`, `.7z`, `.tar`, `.gz` |
| `Instaladores` | `.exe`, `.msi`, `.msix` |
| `Codigo` | `.py`, `.js`, `.ts`, `.html`, `.css`, `.java`, `.cpp` |
| `Otros` | Archivos sin extensión o con extensiones no reconocidas |

Las extensiones se comparan en minúsculas. Una extensión solo puede pertenecer
a una categoría; si aparece duplicada, se conserva la primera y se avisa. Las
reglas se pueden editar desde la interfaz o directamente en `config.json`.

---

## Configuración

### Ubicación

| Elemento | Ruta predeterminada |
| --- | --- |
| Configuración | `%APPDATA%\SmartFileOrganizer\config.json` |
| Historial | `%APPDATA%\SmartFileOrganizer\history.json` |
| Bloqueo | `%APPDATA%\SmartFileOrganizer\organizer.lock` |
| Registro | `%APPDATA%\SmartFileOrganizer\logs\smart_file_organizer.log` |

Los datos de ejecución se guardan **fuera del repositorio** para evitar publicar
rutas personales. Se puede cambiar la ubicación con la variable de entorno
`SMART_FILE_ORGANIZER_HOME`:

```powershell
$env:SMART_FILE_ORGANIZER_HOME = "C:\Datos\SmartFileOrganizer"
python main.py
```

El registro rota a los 2 MB y conserva hasta tres copias. La CLI no imprime los
registros en la consola para mantener limpia la salida; los errores relevantes
sí se muestran por `stderr`.

### Copia de ejemplo

`config/config.example.json` contiene una configuración completa. Para usarla:

```powershell
$destino = "$env:APPDATA\SmartFileOrganizer"
New-Item -ItemType Directory -Force -Path $destino | Out-Null
Copy-Item "config\config.example.json" "$destino\config.json"
python main.py --validate-config
```

Las rutas de ejemplo usan `%USERPROFILE%`; la aplicación expande variables de
entorno y guarda después la ruta resuelta. En una instalación con OneDrive, es
preferible dejar que la detección inicial elija las carpetas reales o
seleccionarlas con **Examinar…**.

### Estructura principal

| Clave | Descripción |
| --- | --- |
| `sources` | Lista de `{name, path, enabled}`. Solo se procesan los habilitados. |
| `categories` | Objeto `categoría: [extensiones]`. |
| `default_category` | Categoría para archivos sin extensión o desconocidos. |
| `automation.enabled` | Activa la organización periódica. **Desactivada por defecto.** |
| `automation.interval_minutes` | Intervalo entre ejecuciones (1–1440). |
| `automation.start_with_windows` | Estado deseado del inicio con Windows. |
| `automation.run_on_start` | Ejecuta una organización al iniciar el programador. |
| `watcher.enabled` | Activa la vigilancia con `watchdog`. |
| `watcher.settle_seconds` | Espera para considerar estable un archivo (0.5–300). |
| `watcher.poll_interval` | Frecuencia de comprobación (0.2–10 s). |
| `history.enabled` | Guarda operaciones para poder restaurarlas. |
| `history.max_operations` | Máximo de operaciones conservadas (1–10000). |
| `safety.skip_hidden` | Omite archivos ocultos o que empiezan por punto. |
| `safety.skip_system` | Omite archivos con atributo de sistema en Windows. |
| `safety.confirm_before_organize` | Confirma antes de organizar desde la GUI. |
| `ui.*` | Tema, tamaño de ventana y última carpeta usada. |

### Validación y recuperación

- La configuración se normaliza al cargar y al guardar: valores fuera de rango
  se ajustan, rutas relativas se descartan, nombres de categoría inseguros se
  ignoran y extensiones duplicadas se resuelven.
- `--validate-config` inspecciona el archivo **sin escribirlo** y muestra
  errores, avisos y valores efectivos.
- Si `config.json` contiene JSON dañado, se crea una copia
  `config.json.corrupt-AAAAMMDD-HHMMSS`, se restaura una configuración segura y
  se informa en la interfaz o en la CLI.
- Si el historial está dañado, se conserva una copia `history.json.corrupt-*` y
  se continúa con un historial vacío.
- Las escrituras son atómicas: se escribe un archivo temporal, se sincroniza y
  se reemplaza el original.

---

## Seguridad y comportamiento de los movimientos

La aplicación aplica estas reglas en cada operación:

1. **Solo archivos regulares del primer nivel.** No entra en subcarpetas, no
   sigue enlaces simbólicos y no toca dispositivos, sockets ni archivos
   especiales.
2. **Sin sobrescrituras.** Antes de mover reserva el nombre de destino con
   `O_CREAT | O_EXCL`. Si ya existe `informe.pdf`, usa `informe_1.pdf`,
   `informe_2.pdf`, etc.
3. **Sin eliminaciones.** No hay borrado de archivos ni de carpetas. En un
   movimiento entre volúmenes se copia con `copy2` y solo se elimina el origen
   después de que la copia haya terminado correctamente.
4. **Sin ejecución.** Los `.exe`, `.msi` y scripts solo se mueven; nunca se
   ejecutan.
5. **Sin administrador.** No se solicitan privilegios elevados, no se modifican
   el registro ni el Programador de tareas.
6. **Dentro del origen.** El destino es `<origen>\<categoría>` y se comprueba que
   la ruta real siga dentro de la raíz permitida. Los nombres de categoría se
   validan para evitar rutas como `..\fuera` o nombres reservados de Windows.
7. **Un proceso a la vez.** Un bloqueo con PID y token impide que dos ventanas,
   la CLI o el programador muevan archivos simultáneamente.
8. **Parada cooperativa.** El botón **Detener proceso** solicita detenerse
   después del archivo actual; no se interrumpe un movimiento a medias.
9. **Confirmación explícita.** La GUI siempre pide confirmación antes de
   organizar y muestra un aviso adicional la primera vez.
10. **Dry-run real.** La simulación no crea carpetas, no abre archivos en modo
    escritura, no adquiere el bloqueo y no modifica el historial.

Los archivos ocultos (nombre que empieza por `.` o atributo oculto de Windows) y
los archivos de sistema se omiten por defecto. Se puede cambiar este
comportamiento con `safety.skip_hidden` y `safety.skip_system`, siendo
consciente de que una carpeta como Descargas puede contener archivos de sistema.

---

## Historial y restauración

Cada organización que mueve al menos un archivo se registra en `history.json`
con:

- identificador único de operación;
- fecha de inicio y fin;
- raíces procesadas;
- por archivo: origen, destino, tamaño, categoría, estado y error si lo hubo;
- marca de cancelación y de restauración.

La restauración:

- comprueba que el archivo siga existiendo en el destino;
- comprueba que la carpeta original siga existiendo;
- comprueba que origen y destino sigan dentro de las raíces registradas;
- **no sobrescribe** si el nombre original está ocupado;
- permite omitir el conflicto o restaurar con un nombre alternativo
  (`informe_1.pdf`);
- solo marca la operación como deshecha cuando todos sus archivos movidos han
  sido restaurados;
- añade al historial una operación de tipo `restore` con el resultado.

El historial **no es una copia de seguridad**. No puede recuperar archivos
borrados o modificados después de la organización, ni deshacer cambios hechos
por otros programas. Para datos importantes, mantén copias de seguridad
independientes.

---

## Automatización y vigilancia

### Organización periódica

Está **desactivada por defecto**. Se puede activar desde la GUI o editando
`config.json`:

```json
"automation": {
  "enabled": true,
  "interval_minutes": 30,
  "start_with_windows": false,
  "run_on_start": false
}
```

Con `enabled: true`, la GUI inicia el programador mientras esté abierta. La CLI
`--schedule` se niega a arrancar si la automatización está desactivada. El
programador:

- usa un hilo separado y un `Event` de parada;
- respeta el bloqueo de un solo proceso;
- registra cada ejecución en el historial;
- no crea tareas de Windows ni se ejecuta si la aplicación está cerrada.

### Vigilancia con watchdog

La vigilancia está desactivada por defecto. Requiere `watchdog`:

```powershell
pip install watchdog
```

Desde la GUI se activa con la casilla **Vigilar archivos nuevos**; desde la CLI:

```powershell
python main.py --watch
```

La vigilancia:

- observa solo el primer nivel de la carpeta seleccionada;
- espera `settle_seconds` para que la copia termine;
- evita procesar dos veces la misma firma de tamaño y fecha;
- se puede pausar y reanudar;
- se detiene de forma limpia al cerrar la aplicación;
- si `watchdog` no está instalado, el resto de la aplicación sigue funcionando
  y la opción aparece deshabilitada con un mensaje claro.

### Inicio con Windows

Desde la GUI o con `--install-startup`, la aplicación crea un acceso directo
`.lnk` en:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

El acceso directo se crea con PowerShell y `WScript.Shell`. Si PowerShell no
está disponible, se usa un lanzador `.cmd` identificable. No se modifica el
registro ni el Programador de tareas. La operación pide consentimiento y
`--yes` solo debe usarse cuando la persona usuaria ya lo haya autorizado.

Activar el inicio con Windows no activa por sí solo la organización automática:
el acceso directo abre la aplicación y la automatización sigue dependiendo de
`automation.enabled`.

---

## Pruebas

Las pruebas usan exclusivamente directorios temporales y no tocan archivos
personales.

```powershell
python -m unittest discover -s tests -v
```

Resultado de la última ejecución en este entorno (Windows, Python 3.12.10):

```
Ran 135 tests

OK (skipped=4)
```

Los 4 saltos son esperados:

- 3 pruebas de enlaces simbólicos se omiten si Windows no concede el privilegio
  para crearlos; la aplicación los rechaza igualmente mediante `lstat`.
- 1 prueba solo aplica cuando `watchdog` **no** está instalado; en este entorno
  sí lo está.

La suite cubre, entre otros:

- clasificación por extensión, mayúsculas, Unicode y extensiones compuestas;
- análisis de PDF, JPG y ZIP sin crear carpetas ni mover archivos;
- carpetas vacías, rutas con espacios y caracteres especiales, y múltiples
  archivos en una sola operación;
- creación de carpetas de destino solo durante el movimiento;
- archivos que desaparecen o cambian durante la operación, sin abortar el lote;
- nombres duplicados sin sobrescritura y fallos específicos al crear el destino;
- normalización, validación y recuperación de configuración dañada;
- movimientos, colisiones, dry-run, errores de permisos y fallback entre
  volúmenes;
- restauración con conflicto, omisión, renombrado y destinos desaparecidos;
- bloqueo entre procesos, parada cooperativa, múltiples orígenes y archivos sin
  extensión;
- comandos de la CLI y códigos de salida;
- detección real de archivos con `watchdog` y programador periódico.

También se verificó manualmente:

- creación e interacción con la ventana principal, el editor de categorías, la
  previsualización y el historial;
- flujo completo de la GUI: analizar, confirmar, mover y restaurar;
- cierre limpio de hilos y ventanas.

---

## Estructura del proyecto

```
.
├── main.py                     # Punto de entrada GUI + CLI
├── src/
│   ├── __init__.py             # Nombre y versión
│   ├── paths.py                # Rutas de datos y detección de carpetas Windows
│   ├── config_manager.py       # Configuración, normalización y recuperación
│   ├── file_classifier.py      # Clasificación por extensión
│   ├── safe_mover.py           # Movimiento seguro y colisiones
│   ├── locking.py              # Bloqueo entre procesos
│   ├── history_manager.py      # Historial y restauración
│   ├── organizer.py            # Análisis, planificación y ejecución
│   ├── scheduler.py            # Organización periódica en un hilo
│   ├── file_watcher.py         # Integración opcional con watchdog
│   ├── autostart.py            # Acceso directo en la carpeta Inicio
│   ├── logging_config.py       # Registro rotativo
│   ├── gui.py                  # Ventana principal de Tkinter
│   └── gui_dialogs.py          # Categorías, previsualización e historial
├── tests/
│   ├── test_classifier.py
│   ├── test_config_manager.py
│   ├── test_safe_mover.py
│   ├── test_history_manager.py
│   ├── test_organizer.py
│   ├── test_cli.py
│   ├── test_file_watcher.py
│   └── test_scheduler.py
├── config/config.example.json
├── docs/screenshots/README.md
├── logs/.gitkeep
├── assets/README.md             # assets/app.ico se genera al compilar
├── tools/make_icon.py           # Generador del icono de Windows
├── SmartFileOrganizer.spec      # PyInstaller onefile
├── SmartFileOrganizer-onedir.spec  # PyInstaller onedir
├── version_info.txt             # Metadatos del .exe
├── requirements.txt
├── requirements-build.txt
├── build.bat
├── LICENSE
└── README.md
```

### Diseño en capas

- **Dominio y seguridad:** `file_classifier`, `safe_mover`, `organizer`,
  `history_manager`.
- **Persistencia e infraestructura:** `config_manager`, `paths`, `locking`,
  `logging_config`.
- **Automatización:** `scheduler`, `file_watcher`, `autostart`.
- **Presentación:** `gui`, `gui_dialogs`, `main`.
- **Pruebas:** `unittest` con directorios temporales y mocks puntuales.

La GUI no mueve archivos directamente: delega en `Organizer`, que a su vez usa
`SafeMover` y `HistoryManager`. Esto permite probar la lógica sin Tkinter y
mantener la interfaz como una capa fina.

---

## Solución de problemas

### `No module named tkinter`

Usa el instalador oficial de Python para Windows y marca la opción **tcl/tk and
IDLE**. En algunas distribuciones mínimas o de Microsoft Store, Tkinter no está
incluido. Comprueba con:

```powershell
python -c "import tkinter; print(tkinter.TkVersion)"
```

### `watchdog` no está instalado

La aplicación funciona sin él. Si quieres vigilancia en tiempo real:

```powershell
pip install watchdog
```

Después reinicia la aplicación. Si la opción sigue deshabilitada, comprueba que
estás usando el mismo intérprete de Python:

```powershell
python -c "import watchdog; print(watchdog.__version__)"
```

### El inicio con Windows no se crea

- Comprueba que PowerShell esté disponible.
- Revisa la carpeta Inicio del usuario; puede existir un `.lnk` o un `.cmd`.
- Si PowerShell está bloqueado por políticas, la aplicación intenta un lanzador
  `.cmd`. También puedes ejecutar la app manualmente.
- No se necesitan permisos de administrador; si el sistema impide escribir en la
  carpeta Inicio, la aplicación muestra el error y no cambia la configuración de
  forma silenciosa.

### Aparece «Ya hay una organización en curso»

Otra ventana, la CLI o el programador tiene el bloqueo. Espera a que termine.
Solo elimina `%APPDATA%\SmartFileOrganizer\organizer.lock` si estás seguro de que
no hay ningún proceso activo; el archivo contiene únicamente PID, equipo y fecha.

### Un archivo no se mueve

Revisa el área de mensajes o `logs\smart_file_organizer.log`. Causas habituales:

- el archivo está abierto o bloqueado por otro programa;
- el archivo es un enlace simbólico o no es un archivo regular;
- el archivo es oculto o de sistema y `skip_hidden`/`skip_system` está activo;
- la carpeta de destino existe como archivo;
- no hay permisos de escritura en el origen o destino;
- el archivo cambió de nombre o desapareció durante la operación.

La aplicación no solicita elevación. Si un archivo requiere permisos
especiales, debes resolverlo manualmente.

### «No se pudo crear la carpeta de destino» o «el sistema no encontró una ruta»

Este mensaje sustituye al antiguo texto genérico «El archivo o alguna de sus
rutas dejó de existir durante la operación». Ahora indica la operación y las
rutas implicadas. Si aparece al organizar una carpeta dentro de `Documents`,
`Desktop`, `Pictures` u otra carpeta protegida:

1. Comprueba que la carpeta de origen siga existiendo y que la unidad esté
   disponible.
2. Revisa **Seguridad de Windows → Protección contra virus y amenazas →
   Protección contra ransomware → Acceso controlado a carpetas**. Si está
   activa, permite `python.exe` o `SmartFileOrganizer.exe`, o utiliza una
   carpeta no protegida como origen (por ejemplo, una subcarpeta de
   `Downloads`).
3. Revisa si un antivirus de terceros bloquea la creación de archivos en
   `Documents`.
4. Comprueba los permisos de la carpeta de destino con clic derecho →
   **Propiedades → Seguridad**.

El análisis es de solo lectura y puede funcionar aunque el movimiento esté
bloqueado; el error se produce al crear la carpeta de destino o al mover. La
aplicación deja intactos los archivos que no puede mover y continúa con el
resto.

### El `.exe` no arranca o se cierra sin mensaje

- Abre `%APPDATA%\SmartFileOrganizer\logs\smart_file_organizer.log`; el error
  fatal se registra allí.
- Ejecuta la variante **onedir** (`dist\SmartFileOrganizer\SmartFileOrganizer.exe`)
  para ver si falta algún archivo de la carpeta.
- Comprueba que no haya otra instancia usando el bloqueo
  `%APPDATA%\SmartFileOrganizer\organizer.lock`.
- Si Windows SmartScreen bloquea el archivo, usa **Más información → Ejecutar de
  todos modos** o firma el ejecutable con un certificado de code signing.
- El `.exe` no incluye Python ni Tkinter: PyInstaller los empaqueta. Si falta
  Tkinter, el error se registra en el log y se muestra un diálogo.

### Errores frecuentes de PyInstaller

| Error | Solución |
| --- | --- |
| `No module named ...` | Instala la dependencia en el **mismo** Python que ejecuta PyInstaller y vuelve a compilar con `--clean`. |
| `PermissionError` al escribir en `dist` | Cierra el `.exe` que esté en ejecución y vuelve a compilar. |
| `tkinter` no encontrado | Usa el instalador oficial de Python con Tcl/Tk; no uses una distribución mínima. |
| `watchdog` no incluido | Instala `watchdog` antes de compilar o deja que la app funcione sin vigilancia. |
| El antivirus bloquea el `.exe` recién creado | Añade la carpeta `dist` a las exclusiones o firma el ejecutable. |
| `failed to collect ...` | Ejecuta `python -m PyInstaller --clean --noconfirm SmartFileOrganizer.spec` y revisa la primera línea del error. |

Para limpiar artefactos de compilación:

```powershell
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

### La configuración se restableció

Si el JSON estaba dañado, se guardó una copia `config.json.corrupt-*` junto al
original. Revisa esa copia, corrige el JSON y vuelve a copiarlo. También puedes
usar `--validate-config` antes de arrancar la GUI.

### Los acentos se ven mal en la consola

La aplicación intenta configurar UTF-8 en `stdout`/`stderr`. Si tu terminal no lo
respeta:

```powershell
chcp 65001
```

Windows Terminal y PowerShell 7 suelen mostrarlo correctamente.

### Las pruebas de enlaces simbólicos se omiten

Es el comportamiento esperado en Windows si el usuario no tiene el privilegio
para crear enlaces simbólicos. No es un fallo de la aplicación: el código
rechaza enlaces mediante `lstat` y las pruebas se saltan cuando el sistema no
permite crearlos.

### OneDrive y archivos en la nube

La detección inicial consulta las carpetas conocidas de Windows, el registro y
las variables de OneDrive. Si tus carpetas están redirigidas, la app debería
encontrarlas. Los archivos que solo existen en la nube pueden descargarse al
moverlos; esto depende de la configuración de OneDrive y no de la aplicación.

---

## Limitaciones

- Solo procesa el **primer nivel** de cada origen. No organiza subcarpetas
  existentes ni aplica reglas recursivas.
- La clasificación es por **extensión**, no por contenido. No detecta archivos
  mal nombrados ni analiza el interior de los documentos.
- No busca duplicados por contenido ni calcula hashes.
- No es una herramienta de copia de seguridad. El historial permite deshacer
  movimientos, no recuperar archivos eliminados o modificados.
- La organización automática solo funciona mientras la aplicación está abierta.
  No se instalan servicios ni tareas programadas de Windows.
- La vigilancia en tiempo real requiere `watchdog` y observa un solo nivel.
- El inicio con Windows depende de la carpeta Inicio y de PowerShell; en
  entornos muy restringidos puede no estar disponible.
- La interfaz está optimizada para Windows y pantallas de escritorio. No es una
  aplicación web ni móvil.
- Los movimientos entre volúmenes usan copia y borrado del origen; en archivos
  muy grandes el progreso se muestra por archivo, no por bytes.
- No se ejecutan archivos ni se analiza malware. Mover un `.exe` no lo hace
  seguro.

---

## Privacidad

- No hay telemetría, cuentas, red ni servicios en la nube.
- El historial guarda rutas, tamaños, categorías y estados; **no guarda el
  contenido** de los archivos.
- La configuración, el historial, el bloqueo y los registros se guardan en el
  perfil del usuario, fuera del repositorio.
- El único uso de PowerShell es crear o eliminar el acceso directo de inicio,
  y solo cuando la persona usuaria lo solicita.
- Las capturas de ejemplo deben usar archivos ficticios y ocultar rutas
  personales.

---

## Licencia

Distribuido bajo la licencia **MIT**. Consulta [LICENSE](LICENSE).

---

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python main.py                  # GUI
python main.py --analyze        # inspect configured sources
python main.py --dry-run        # simulate
python main.py --organize       # move files safely
python main.py --stats          # history statistics
python main.py --validate-config
python main.py --undo --yes     # restore the last operation
python -m unittest discover -s tests -v
```

Smart File Organizer never overwrites or deletes files, never executes them and
never asks for administrator rights. Only top-level regular files are
processed; symlinks, directories and non-regular files are ignored. The history
is an undo aid, not a backup.
