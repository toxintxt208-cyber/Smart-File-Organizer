# Recursos de compilación

`app.ico` se genera automáticamente al ejecutar `build.bat` mediante
`tools/make_icon.py`. No es necesario en tiempo de ejecución: la aplicación
funciona igual si el icono no está presente (PyInstaller usa el icono por
defecto de Python).

Para generarlo manualmente:

```bat
python tools\make_icon.py
```

Requiere Pillow, incluido en `requirements-build.txt`.
