# Whisper Vulkan (AMD / Intel) — whisper.cpp

Instalação: `bash instalar.sh` · Teste: `bash testar.sh`

## Versões fixadas
- whisper.cpp **v1.9.5** (clone raso; `-DGGML_VULKAN=1 -DBUILD_SHARED_LIBS=OFF`, alvo `whisper-server`). Reserva: v1.8.4 (`9386f239`), compila sem `spirv-headers`. O backend CPU usa `-march=native`: **o binário não é portátil entre CPUs diferentes — compile em cada máquina**.
- Modelo f16 `ggml-large-v3-turbo.bin` (1 624 555 275 B) sha256 `1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69` — `huggingface.co/ggerganov/whisper.cpp`.

## Unidades (`systemd --user`)
| Unidade | Papel |
|---|---|
| `whisper-vulkan.service` (+ `.d/10-espera-dri.conf`) | `whisper-server` f16 em `127.0.0.1:47111`, flags `-l pt -t 8 -fa -bs 2`; o drop-in espera `/dev/dri/renderD128` ficar gravável (até 60 s após o login) |
| `whisper-ditado.service` (+ `.d/perfil.conf`) | daemon (`whisper_daemon.py`) apontando para o servidor Vulkan |

## Armadilhas
- **"no GPU found" em silêncio** (bug real, já preso): o servidor sobe e roda em CPU, 5–10× mais lento. `testar.sh` confere o log.
- Usuário precisa de acesso a `/dev/dri/renderD128` (grupo `render`).
- `-t 8` é fixo na unit; o `instalar.sh` reduz para `nproc` em máquina com menos núcleos.
- llvmpipe (Vulkan por CPU) **não conta** como GPU.
- `HF_HUB_OFFLINE=1` no perfil: o faster-whisper de reserva (venv CPU) precisa do cache `~/.cache/huggingface` (copiar de outra máquina ou baixar uma vez com a variável desligada).

Latência medida (AMD 780M): **1,0–1,9 s** para transcrever 5–30 s de fala; o piso (~1,2 s) é o encoder do Whisper (janela fixa de 30 s).
