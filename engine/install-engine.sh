#!/usr/bin/env bash
# Instalador do motor Whisper local — idempotente, sem sudo (pacotes apt faltando: mostra o comando e para).
# Perfis (detecção automática, ou --perfil):
#   cuda   GPU NVIDIA (nvidia-smi -L): faster-whisper no venv (requirements no engine/requirements_whisper.txt).
#   vulkan sem NVIDIA, com GPU Vulkan real (vulkaninfo: integrada/discreta, não llvmpipe):
#          venv CPU (reserva do daemon) + whisper.cpp compilado com Vulkan em ~/.local/opt/whisper.cpp,
#          modelo ggml em ~/.local/share/whisper.cpp/models e as units whisper-vulkan + whisper-ditado.
#   cpu    sem GPU utilizável: requisitos_cpu.txt e drop-in do perfil CPU.
# Instala em $HOME/.local/share/whisper-ditado (código + venv) e as unidades systemd --user
# (whisper-ditado, ditado-stream e, no vulkan, whisper-vulkan).
# Uso: bash install-engine.sh [--perfil cpu|cuda|vulkan] [--sem-servicos] [--so-motor]
#   --so-motor  (perfil vulkan) instala só whisper.cpp + modelo + whisper-vulkan.service e para.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"          # engine/
RAIZ="$(cd "$AQUI/.." && pwd)"                 # raiz do repositório
INST="$HOME/.local/share/whisper-ditado"       # código + venv instalados
VENV="$INST/venv"
UNID="$HOME/.config/systemd/user"
MODELOS=("deepdml/faster-whisper-large-v3-turbo-ct2" "small")   # final (daemon) + parciais (ditado-stream)

# --- whisper.cpp do perfil vulkan: versão FIXADA. A v1.8.4 (9386f239) é a reserva testada que compila
#     sem spirv-headers; da v1.8.5 em diante o pacote spirv-headers é obrigatório.
WCPP_TAG="v1.9.5"
WCPP_REPO="https://github.com/ggml-org/whisper.cpp"
WCPP_DIR="$HOME/.local/opt/whisper.cpp"
WCPP_BIN="$WCPP_DIR/bin/whisper-server"
GGML_DIR="$HOME/.local/share/whisper.cpp/models"
GGML_NOME="ggml-large-v3-turbo.bin"                          # f16, 1,62 GB (modo qualidade)
GGML_SHA256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
GGML_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$GGML_NOME"
PORTA_VULKAN=47111
APT_VULKAN="cmake build-essential git libvulkan-dev glslc spirv-headers vulkan-tools"

PERFIL="" ; SERVICOS=1 ; SO_MOTOR=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --perfil) PERFIL="$2"; shift 2 ;;
    --sem-servicos) SERVICOS=0; shift ;;
    --so-motor) SO_MOTOR=1; shift ;;
    *) echo "opção desconhecida: $1" >&2; exit 2 ;;
  esac
done
msg() { printf '[whisper-ditado] %s\n' "$*"; }

# ---------------------------------------------------------------------------------------------------------------
# tem_gpu_vulkan: verdadeiro se o vulkaninfo enxerga uma GPU real (integrada ou discreta). O llvmpipe (CPU
# emulando Vulkan) não conta: com ele o whisper.cpp fica mais lento que o faster-whisper em CPU.
# ---------------------------------------------------------------------------------------------------------------
tem_gpu_vulkan() {
  command -v vulkaninfo >/dev/null &&
    vulkaninfo --summary 2>/dev/null | grep -Eq 'PHYSICAL_DEVICE_TYPE_(INTEGRATED|DISCRETE)_GPU'
}

if [[ -z "$PERFIL" ]]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then PERFIL=cuda
  elif tem_gpu_vulkan; then PERFIL=vulkan
  else PERFIL=cpu; fi
fi
[[ "$PERFIL" == cpu || "$PERFIL" == cuda || "$PERFIL" == vulkan ]] || { echo "perfil inválido: $PERFIL" >&2; exit 1; }
((SO_MOTOR)) && [[ "$PERFIL" != vulkan ]] && { echo "--so-motor só vale no perfil vulkan" >&2; exit 1; }
REQ="$AQUI/requisitos_cpu.txt"; [[ "$PERFIL" == cuda ]] && REQ="$AQUI/requirements_whisper.txt"
msg "perfil=$PERFIL requisitos=$(basename "$REQ")"

# ---------------------------------------------------------------------------------------------------------------
# compilar_whisper_cpp: clona/atualiza o whisper.cpp na tag WCPP_TAG e compila só o whisper-server, estático
# (sem .so para achar em tempo de execução), com GGML_VULKAN. Pula tudo se o binário instalado já é dessa tag
# (arquivo VERSAO). Pacotes de compilação faltando → avisa o comando apt e para.
# ---------------------------------------------------------------------------------------------------------------
compilar_whisper_cpp() {
  if [[ -x "$WCPP_BIN" && "$(cat "$WCPP_DIR/VERSAO" 2>/dev/null)" == "$WCPP_TAG" ]]; then
    msg "whisper.cpp $WCPP_TAG já instalado em $WCPP_DIR"; return 0
  fi
  local falta=()
  for p in $APT_VULKAN; do dpkg -s "$p" >/dev/null 2>&1 || falta+=("$p"); done
  if ((${#falta[@]})); then
    msg "faltam pacotes para compilar o whisper.cpp: ${falta[*]}"
    msg "instale com: sudo apt install ${falta[*]}"; exit 1
  fi
  local fonte="$WCPP_DIR/fonte"
  mkdir -p "$WCPP_DIR/bin"
  if [[ ! -d "$fonte/.git" ]]; then
    msg "clonando whisper.cpp $WCPP_TAG..."
    git clone -q --depth 1 --branch "$WCPP_TAG" "$WCPP_REPO" "$fonte"
  else
    git -C "$fonte" fetch -q --depth 1 origin tag "$WCPP_TAG"
    git -C "$fonte" checkout -q "$WCPP_TAG"
  fi
  msg "compilando whisper-server $WCPP_TAG com Vulkan (alguns minutos)..."
  rm -rf "$fonte/build"
  cmake -S "$fonte" -B "$fonte/build" -DGGML_VULKAN=1 -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
        -DWHISPER_BUILD_TESTS=OFF >"$WCPP_DIR/cmake.log" 2>&1 || { msg "cmake falhou: $WCPP_DIR/cmake.log"; exit 1; }
  cmake --build "$fonte/build" -j "$(nproc)" --config Release --target whisper-server >"$WCPP_DIR/build.log" 2>&1 \
    || { msg "compilação falhou: $WCPP_DIR/build.log"; exit 1; }
  install -m 755 "$fonte/build/bin/whisper-server" "$WCPP_BIN"
  printf '%s\n' "$WCPP_TAG" > "$WCPP_DIR/VERSAO"
  msg "whisper-server $WCPP_TAG instalado ($(git -C "$fonte" rev-parse --short HEAD))"
}

# ---------------------------------------------------------------------------------------------------------------
# semear_modelo_ggml: põe o ggml-large-v3-turbo.bin (f16) em GGML_DIR, conferindo o sha256.
# ---------------------------------------------------------------------------------------------------------------
semear_modelo_ggml() {
  local alvo="$GGML_DIR/$GGML_NOME"
  mkdir -p "$GGML_DIR"
  if [[ -f "$alvo" && -f "$alvo.sha256ok" && "$(cat "$alvo.sha256ok")" == "$GGML_SHA256" ]]; then
    msg "modelo ggml em $alvo"; return 0
  fi
  if [[ ! -f "$alvo" ]]; then
    msg "baixando $GGML_NOME (1,6 GB)..."; curl -fsSL -o "$alvo.part" "$GGML_URL"
    mv "$alvo.part" "$alvo"
  fi
  if [[ "$(sha256sum "$alvo" | cut -d' ' -f1)" != "$GGML_SHA256" ]]; then
    msg "sha256 de $alvo não confere — apague o arquivo e rode de novo"; exit 1
  fi
  printf '%s\n' "$GGML_SHA256" > "$alvo.sha256ok"
  msg "modelo ggml conferido (sha256)"
}

# ---------------------------------------------------------------------------------------------------------------
# instalar_unit_vulkan: instala e liga a whisper-vulkan.service e espera o /health responder (até 60 s).
# ---------------------------------------------------------------------------------------------------------------
instalar_unit_vulkan() {
  mkdir -p "$UNID"
  install -m 644 "$AQUI/systemd/whisper-vulkan.service" "$UNID/"
  mkdir -p "$UNID/whisper-vulkan.service.d"
  install -m 644 "$AQUI/systemd/whisper-vulkan.service.d/10-espera-dri.conf" "$UNID/whisper-vulkan.service.d/"
  systemctl --user daemon-reload
  systemctl --user enable whisper-vulkan.service >/dev/null 2>&1
  if ! systemctl --user is-active -q whisper-vulkan.service; then
    systemctl --user restart whisper-vulkan.service
  fi
  for _ in $(seq 1 60); do
    curl -fs "http://127.0.0.1:$PORTA_VULKAN/health" >/dev/null 2>&1 && break; sleep 1
  done
  msg "whisper-vulkan: $(systemctl --user is-active whisper-vulkan) /health=$(curl -fs "http://127.0.0.1:$PORTA_VULKAN/health" 2>/dev/null || echo fora)"
}

if [[ "$PERFIL" == vulkan ]]; then
  tem_gpu_vulkan || msg "AVISO: vulkaninfo não mostra GPU real; o whisper.cpp pode cair no llvmpipe (lento)"
  compilar_whisper_cpp
  semear_modelo_ggml
  if ((SO_MOTOR)); then
    ((SERVICOS)) && instalar_unit_vulkan
    msg "pronto (só o motor vulkan)"; exit 0
  fi
fi

# 1. dependências de sistema (só avisa: instalar exige sudo)
falta=()
for b in arecord ffmpeg xdotool; do command -v "$b" >/dev/null || falta+=("$b"); done
if ((${#falta[@]})); then
  msg "AVISO faltam: ${falta[*]} -> instale com: sudo apt install alsa-utils ffmpeg xdotool"
fi

# 2. venv (recria só se os requisitos mudaram)
if [[ ! -x "$VENV/bin/python3" ]]; then
  python3 -m venv "$VENV" || { msg "python3 -m venv falhou -> sudo apt install python3-venv"; exit 1; }
fi
if ! cmp -s "$REQ" "$VENV/.requisitos_instalados" 2>/dev/null; then
  msg "instalando $(basename "$REQ") no venv..."
  "$VENV/bin/python3" -m pip install -q --upgrade pip
  "$VENV/bin/python3" -m pip install -q -r "$REQ"
  cp "$REQ" "$VENV/.requisitos_instalados"
fi
"$VENV/bin/python3" -c 'import faster_whisper, av; assert av.__version__ == "18.1.0", av.__version__' \
  || { msg "venv sem faster_whisper ou com av errado"; exit 1; }
msg "venv ok ($(du -sh "$VENV" | cut -f1))"

# 3. modelos: usa o cache HF; se faltar, baixa. No perfil vulkan o turbo do faster-whisper é a
#    reserva do daemon (servidor fora) e o small faz as parciais.
for m in "${MODELOS[@]}"; do
  if HF_HUB_OFFLINE=1 "$VENV/bin/python3" -c 'import sys; from faster_whisper import download_model; download_model(sys.argv[1], local_files_only=True)' "$m" >/dev/null 2>&1; then
    msg "modelo em cache: $m"
  else
    msg "baixando modelo: $m"
    "$VENV/bin/python3" -c 'import sys; from faster_whisper import download_model; download_model(sys.argv[1])' "$m" >/dev/null
  fi
done

# 4. código instalado: daemon + serviço de ditado (o que roda é a cópia aqui, não o repositório)
mkdir -p "$INST"
install -m 755 "$AQUI/whisper_daemon.py" "$INST/"
for f in ditado_stream.py dicionario.py dicionario.yaml aprender.py polir.py; do
  install -m 644 "$RAIZ/ditado/$f" "$INST/"
done
msg "código em $INST"

((SERVICOS)) || { msg "pronto (sem serviços)"; exit 0; }

# 5. unidades + drop-ins dos perfis cpu e vulkan
mkdir -p "$UNID"
install -m 644 "$AQUI/systemd/whisper-ditado.service" "$UNID/"
install -m 644 "$AQUI/systemd/ditado-stream.service" "$UNID/"
if [[ "$PERFIL" == cpu || "$PERFIL" == vulkan ]]; then
  mkdir -p "$UNID/whisper-ditado.service.d" "$UNID/ditado-stream.service.d"
  if [[ "$PERFIL" == vulkan ]]; then
    # Daemon vira cliente do whisper-server; reserva = turbo em CPU sob demanda (8 threads).
    printf '[Unit]\n# perfil vulkan (install-engine.sh)\nWants=whisper-vulkan.service\nAfter=whisper-vulkan.service\n\n[Service]\nEnvironment=WHISPER_DITADO_DISPOSITIVO=vulkan\nEnvironment=WHISPER_DITADO_SERVIDOR=http://127.0.0.1:%s\nEnvironment=WHISPER_DITADO_THREADS=8\nEnvironment=HF_HUB_OFFLINE=1\n' \
      "$PORTA_VULKAN" > "$UNID/whisper-ditado.service.d/perfil.conf"
  else
    printf '[Service]\n# perfil CPU (install-engine.sh)\nEnvironment=WHISPER_DITADO_DISPOSITIVO=cpu\nEnvironment=WHISPER_DITADO_THREADS=8\nEnvironment=HF_HUB_OFFLINE=1\n' \
      > "$UNID/whisper-ditado.service.d/perfil.conf"
  fi
  # Parciais: small na CPU nos dois perfis (no servidor elas disputariam a fila com o final).
  printf '[Service]\n# perfil %s (install-engine.sh): parciais small na CPU\nEnvironment=DITADO_DISPOSITIVO=cpu\nEnvironment=DITADO_THREADS=4\nEnvironment=HF_HUB_OFFLINE=1\n' \
    "$PERFIL" > "$UNID/ditado-stream.service.d/perfil.conf"
  msg "drop-ins do perfil $PERFIL gravados"
fi
[[ "$PERFIL" == vulkan ]] && instalar_unit_vulkan
# Fora do perfil vulkan, o whisper-server não tem cliente: desliga e libera ~2 GB de GTT.
if [[ "$PERFIL" != vulkan ]] && systemctl --user is-enabled --quiet whisper-vulkan.service 2>/dev/null; then
  systemctl --user disable --now whisper-vulkan.service >/dev/null 2>&1 && msg "whisper-vulkan desligado (perfil $PERFIL)"
fi
systemctl --user daemon-reload
systemctl --user enable whisper-ditado.service ditado-stream.service >/dev/null 2>&1
systemctl --user restart whisper-ditado.service
systemctl --user restart ditado-stream.service
for i in $(seq 1 30); do [[ -S /tmp/whisper_ditado.sock && -S /tmp/ditado_stream.sock ]] && break; sleep 1; done
for s in whisper-ditado ditado-stream; do msg "$s: $(systemctl --user is-active $s)"; done
journalctl --user -u whisper-ditado -n 20 --no-pager -o cat | grep -E 'pronto|FALHA' | tail -1 || true
