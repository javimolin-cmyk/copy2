# Clonador de sitios (GitHub Pages + Actions)

Pegas un enlace en la página y se crea una copia completa en `copias/<sitio>/` de este mismo
repositorio, publicada con GitHub Pages. La copia la hace GitHub Actions con un Chromium real, así
que guarda lo que aparece al inspeccionar la página: el DOM ya renderizado por JavaScript, todo el
tráfico de red (HTML, CSS, JS, imágenes, fuentes, vídeo, JSON de fetch/XHR) y las subpáginas.

## Configuración (una sola vez)

1. Crea un repositorio **público** y sube todo el contenido de esta carpeta (incluida `.github/`).
   Si el navegador no sube la carpeta oculta `.github`, crea en GitHub el archivo
   `.github/workflows/clonar.yml` con "Add file → Create new file" y pega su contenido.
2. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, carpeta `/ (root)`.
3. **Settings → Actions → General → Workflow permissions**: *Read and write permissions*.
4. Token: clásico con permisos `repo` y `workflow` (o fine-grained con *Actions* y *Contents* en lectura/escritura sobre este repositorio).
5. Abre `https://TU_USUARIO.github.io/NOMBRE_DEL_REPO/`, pega el enlace y el token y pulsa **Crear copia**.

La copia aparece en `https://TU_USUARIO.github.io/NOMBRE_DEL_REPO/copias/<sitio>/`.
El token solo se envía a la API de GitHub; se guarda en el navegador únicamente si marcas "Recordar".

## Modos

- **Instantánea** (por defecto): guarda el DOM tal como se ve, sin scripts. Es la más fiel y la que mejor funciona en Pages.
- **Interactivo**: conserva los scripts y redirige `fetch`/XHR a las respuestas guardadas. Funciona en sitios simples; las aplicaciones con router propio (React, Vue…) pueden fallar.

## Qué hace la copia

- Carga cada página en Chromium, hace scroll para activar la carga diferida y pulsa pestañas, acordeones y menús que no cambian de página.
- Registra todo lo que la página pide a la red y lo guarda; en `_w2r/red.json` queda el listado.
- Reescribe los enlaces a rutas relativas. Los recursos de otros dominios van a `_ext/<dominio>/`.
- Desactiva los formularios de la copia y añade a cada página un aviso de "copia archivada".

## Límites reales

Es gratis con repositorio público, pero GitHub impone sus propios topes: archivos de hasta 100 MB
(aquí se omiten los de más de 50 MB), sitio de Pages de hasta 1 GB, 100 GB de ancho de banda al mes
y 6 horas por ejecución. Por eso la herramienta se detiene a los 800 MB o 240 minutos (ajustable en
`scripts/clone.py`). No puede copiar páginas que exijan iniciar sesión ni el código del servidor
(PHP, bases de datos, APIs con POST).

Úsalo con sitios tuyos o con permiso del autor, y respeta `robots.txt` y los derechos de autor.
