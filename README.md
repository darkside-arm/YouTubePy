# YouTube para R36S / R36T (ArkOS + PortMaster)

Cliente de YouTube en Python + SDL2 para consolas retro tipo R36S/R36T.
Feed personalizado, busqueda instantanea, favoritos, historial y
reproduccion con mpv. No instala nada en el sistema: todo vive en la
carpeta del port.

## Requisitos (ya presentes en ArkOS)
- Python 3.7+, SDL2 / SDL2_ttf / SDL2_image (SDL2_gfx opcional, para
  esquinas redondeadas), mpv, y una fuente DejaVu.
- La libreria PySDL2 va INCLUIDA (carpeta `sdl2/`, Python puro).

## Instalacion
1. Copiar la carpeta `youtube_py/` a `/roms/ports/youtube_py/`
2. Copiar `YouTubePy.sh` a `/roms/ports/YouTubePy.sh`
3. Descargar el binario de yt-dlp para ARM64:
   https://github.com/yt-dlp/yt-dlp/releases (archivo `yt-dlp_linux_aarch64`)
   y guardarlo como `/roms/ports/youtube_py/yt-dlp.real` (con permisos de
   ejecucion: `chmod +x yt-dlp.real`)
4. Refrescar la lista de juegos. Listo: funciona sin cuenta.

## Sesion de YouTube (opcional, para feed personalizado)
1. En el PC, abrir una ventana de INCOGNITO -> youtube.com -> iniciar sesion
2. Exportar cookies con la extension "Get cookies.txt LOCALLY"
   (formato Netscape)
3. Cerrar el incognito SIN cerrar sesion (evita que Google rote los tokens)
4. Copiar el archivo a la consola:
   `scp cookies.txt ark@<IP>:/roms/ports/youtube_py/cookies.txt`

Cuando caduquen, la app muestra estas mismas instrucciones en pantalla.
La reproduccion siempre va sin cookies (evita el bloqueo por PO token);
solo el feed y las busquedas usan la sesion.

## Controles
- Dpad: navegar (izquierda entra al menu lateral)
- A: seleccionar / reproducir / escribir (teclado)
- B: atras / salir del video / salir de la app / borrar (teclado)
- X: busqueda   Y: favorito   START (en teclado): buscar

## Configuracion (`config.json`)
- `ui.margin_x` / `ui.margin_y`: margen del bisel en px. Por defecto
  `"auto"`: la app lee `/proc/device-tree/model` y usa 35 px en
  R36T/K36S (el marco tipo TV tapa los bordes del panel) y 8 px en el
  resto (R36S y similares). Poner un numero fijo para forzarlo.
- `video.mpv_args`: filtro de escalado del video. Por defecto llena la
  pantalla recortando lo que sobre, centrado, para cualquier aspect ratio.
- `quality`: altura maxima del stream (480 recomendado).

## Notas
- Cache de thumbnails autolimitada a 300 archivos (~3 MB).
- Si YouTube deja de reproducir en el futuro, basta actualizar
  `yt-dlp.real` a la ultima version.
