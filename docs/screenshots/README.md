# Capturas de pantalla

Esta carpeta está reservada para las capturas que se muestren en el README.
El repositorio no incluye imágenes generadas automáticamente para no publicar
nombres de archivos ni rutas personales.

## Capturas recomendadas

1. **Ventana principal** (`main-window.png`): con una carpeta de demostración
   seleccionada y la tabla de archivos clasificados.
2. **Previsualización** (`preview.png`): antes de organizar, mostrando categorías
   y destinos calculados.
3. **Historial** (`history.png`): operación completada con detalle de archivos.
4. **Categorías** (`categories.png`): editor de reglas y extensiones.
5. **Restauración** (`restore.png`): resultado de restaurar el último movimiento.

## Cómo generarlas

1. Crea una carpeta de demostración con archivos ficticios:
   `informe.pdf`, `presupuesto.xlsx`, `foto.png`, `video.mp4`, `LEEME`.
2. Ejecuta `python main.py`.
3. Usa el botón **Examinar…** para seleccionar esa carpeta.
4. Pulsa **Analizar archivos** y captura la ventana con `Win + Shift + S`.
5. Repite con **Previsualizar cambios**, **Ver historial** y el editor de
   categorías.
6. Guarda las imágenes en esta carpeta con los nombres indicados.
7. Añade las imágenes al README con rutas relativas:

   ```markdown
   ![Ventana principal](docs/screenshots/main-window.png)
   ```

## Buenas prácticas

- Usa únicamente archivos y carpetas de demostración.
- Oculta nombres de usuario, rutas personales, correos y datos reales.
- Mantén un tamaño legible (por ejemplo, 1200 × 820 px).
- Comprueba que la captura no muestre notificaciones ni otras ventanas.
