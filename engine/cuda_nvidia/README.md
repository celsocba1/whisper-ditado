# Whisper CUDA (NVIDIA) — faster-whisper

Instalação: `bash instalar.sh` (só o driver NVIDIA é pré-requisito). Versões fixadas em [`requirements_whisper.txt`](requirements_whisper.txt): faster-whisper 1.2.1, ctranslate2 4.8.2, numpy 2.5.3, av 18.1.0, nvidia-cublas-cu12 12.9.2.10, nvidia-cudnn-cu12 9.25.1.1, nvidia-cuda-nvrtc-cu12 12.9.86.

- O daemon e o `ditado_stream.py` acrescentam `nvidia/*/lib` do venv ao `LD_LIBRARY_PATH` e se reexecutam sozinhos.
- Modelo `deepdml/faster-whisper-large-v3-turbo-ct2` (int8) + `small` (parciais, só com `DITADO_PREVIA=1`), no cache `~/.cache/huggingface`.
- Sem drop-in de perfil nas unidades: `whisper-ditado.service` padrão.
- Medido numa GTX 1050 Ti (4 GB): frase curta ~2 s.
