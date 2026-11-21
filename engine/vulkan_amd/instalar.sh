#!/usr/bin/env bash
# Whisper local com GPU AMD/Intel via Vulkan (whisper.cpp) — instalação completa em máquina nova.
# Idempotente, sem sudo (se faltar pacote apt, mostra o comando e para). Etapas:
#   1. pacotes e GPU Vulkan real (não llvmpipe)  2. whisper.cpp v1.9.5 compilado com Vulkan  3. modelos ggml (sha256 conferido)
#   4. venv + serviços systemd --user (whisper-vulkan, whisper-ditado, ditado-stream)  5. teste de fumaça (testar.sh)
# Uso: bash instalar.sh
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
UNID="$HOME/.config/systemd/user"

command -v vulkaninfo >/dev/null || { echo "falta vulkaninfo: sudo apt install cmake build-essential git libvulkan-dev glslc spirv-headers vulkan-tools alsa-utils ffmpeg xdotool"; exit 1; }
[[ -w /dev/dri/renderD128 ]] || echo "[aviso] /dev/dri/renderD128 sem escrita: adicione o usuário ao grupo render (sudo usermod -aG render \$USER) e faça login de novo"

# Etapas 1-4: o instalador do motor (compila, baixa o f16 com sha256, cria o venv e as unidades base).
bash "$AQUI/../install-engine.sh" --perfil vulkan

# Threads = núcleos da máquina (o instalador deixa -t 8 fixo na unit).
N=$(nproc); [[ $N -lt 8 ]] && sed -i "s/ -t 8 / -t $N /" "$UNID/whisper-vulkan.service" && echo "[vulkan] -t ajustado para $N"

systemctl --user daemon-reload
systemctl --user restart whisper-vulkan.service
bash "$AQUI/testar.sh"
