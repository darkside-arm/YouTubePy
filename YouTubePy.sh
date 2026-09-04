#!/bin/bash
# Launcher del port YouTube (version Python)

if [ -d "/opt/system/Tools/PortMaster/" ]; then
  controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then
  controlfolder="/opt/tools/PortMaster"
else
  controlfolder="/roms/ports/PortMaster"
fi
[ -f "$controlfolder/control.txt" ] && source "$controlfolder/control.txt"
[ -z "$ESUDO" ] && command -v sudo >/dev/null 2>&1 && ESUDO="sudo"

GAMEDIR="/roms/ports/youtube_py"
cd "$GAMEDIR" || exit 1

[ -n "$sdl_controllerconfig" ] && export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"
export PYSDL2_DLL_PATH="system"

$ESUDO chmod 666 /dev/tty0 2>/dev/null
printf "\033c" > /dev/tty0 2>/dev/null

$ESUDO env SDL_GAMECONTROLLERCONFIG="$SDL_GAMECONTROLLERCONFIG" \
      PYSDL2_DLL_PATH=system \
      python3 "$GAMEDIR/main.py" > "$GAMEDIR/log.txt" 2>&1

printf "\033c" > /dev/tty0 2>/dev/null
