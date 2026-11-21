# whisper-ditado — local, GPU-accelerated voice dictation for OpenCode

**100% local. No cloud, no API keys, no audio leaving your machine.** Real-time dictation (pt-BR focused, works in any language Whisper supports) for the OpenCode Desktop app and TUI — plus a global hotkey that works in *any* window.

| GPU | Engine | Typical latency (end of speech → final text) |
|---|---|---|
| AMD/Intel (Vulkan) | whisper.cpp v1.9.5 + Vulkan, `large-v3-turbo` f16 | **1.0–1.9 s** (5–30 s of speech) |
| NVIDIA (CUDA) | faster-whisper + CTranslate2, `large-v3-turbo` int8 | ~2 s |
| none | faster-whisper int8 on CPU | ~0.2 s per second of audio |

A **user dictionary** fixes technical terms and proper names deterministically (layer 1), feeds them to Whisper as prompt glossary (layer 2, measured: WER 6.8% vs 13.7% with a raw word list), optionally gets a local-LLM polish pass that only *suggests* corrections (never rewrites), and **learns from your own edits** after you send the message twice with the same fix.

> Em português do Brasil? [Leia o resumo em pt-BR](#resumo-em-português-brasil) — o código e os comentários estão em pt-BR.

---

## How it fits together

```
microphone ─► ditado_stream.py ─► whisper_daemon.py (unix socket, protocol v2, prompt glossary)
                                      └─► whisper-server (whisper.cpp + Vulkan, 127.0.0.1:47111)   ← AMD/Intel
                                      └─► faster-whisper CUDA, in-process                          ← NVIDIA
                                      └─► faster-whisper CPU (on-demand fallback)                  ← any
```

- **`engine/`** — the transcription engine: one daemon (`whisper_daemon.py`), three profiles (`auto` → CUDA → Vulkan → CPU), systemd user units, and per-variant installers with pinned versions + sha256-verified models.
- **`ditado/`** — the streaming dictation service (`ditado_stream.py`), the 4-layer correction stack (`dicionario.yaml`, `aprender.py`, `polir.py`) and the global F9 dictation tool.
- **`plugin-desktop/`** — OpenCode **Desktop** plugin: F8 toggle and a **Mic pill** above the composer with a live countdown (`● 00:07 / 03:00`, warning under 30 s), partial text written into the composer while you speak, hallucination filter, and learning hook.
- **`plugin-tui/`** — OpenCode **TUI** plugin: F8, live partials in the prompt, a mic LED next to the prompt (green `● MIC ON`, gray off, red unavailable) and an emergency stop (`/ditar-parar`).

## Install (Linux)

```bash
git clone <this repo> && cd opencode-whisper-ditado
sudo apt install cmake build-essential git libvulkan-dev glslc spirv-headers vulkan-tools \
                 alsa-utils ffmpeg xdotool python3-venv   # vulkan-only build deps; safe to paste anyway
bash install.sh                 # detects GPU: NVIDIA → CUDA, AMD/Intel → Vulkan, none → CPU
```

The installer is idempotent and sudo-free (it tells you the exact `apt` line if something is missing). It installs code + venv into `~/.local/share/whisper-ditado`, compiles whisper.cpp v1.9.5 (Vulkan only), downloads models with sha256 verification, and enables:

- `whisper-vulkan.service` — resident whisper.cpp server on `127.0.0.1:47111` (Vulkan only)
- `whisper-ditado.service` — the shared daemon
- `ditado-stream.service` — the streaming dictation service

Smoke test (Vulkan): `bash engine/vulkan_amd/testar.sh` — checks the service is up, **that it didn't silently fall back to CPU** (`"no GPU found"` is a real, caught-in-the-wild bug), and prints a measured transcription latency.

## OpenCode integration

**Desktop** (`plugin-desktop/`, built bundle included):
1. Add the absolute path of `plugin-desktop/ditado_desktop` to the `plugins` key of `~/.config/opencode/opencode.jsonc`.
2. **Close and reopen OpenCode Desktop** (the plugin loads on startup).
3. Use **F8** or the **Mic** pill. Dictate, stop, the text lands in the composer; press Enter to send (this also feeds the learning layer).

**TUI** (`plugin-tui/`): copy `ditado.tsx` to `~/.config/opencode/tui-plugins/` and register it in `tui.json`. Details in the file header.

**Outside OpenCode**: `python3 ditado/ditar.py` — a floating window on a global hotkey (F9) that transcribes and pastes into whatever window has focus. Works with any app.

## Customizing your dictionary

Edit `ditado/dicionario.yaml` — this is the single most impactful tweak. Proper names and domain jargon are what Whisper gets wrong; the file maps `correct: [misspelled variants...]` and:

- correct forms go into the Whisper prompt (cap 224 tokens);
- misspelled variants are replaced deterministically, whole-word, case-insensitive;
- two occurrences of a manual fix are promoted into the file automatically (`aprender.py`);
- `dica: false` keeps a term out of the prompt when it pulls the model toward a similar word.

`engine/whisper_daemon.py` also has a `DICAS_BASE` constant (always-on prompt terms) you can extend.

Optional LLM polish (layer 2, off by default unless configured): point `DITADO_LLM_URL` at any OpenAI-compatible local endpoint (Ollama, llama.cpp server, LM Studio…). It only returns `wrong => right` corrections and never rewrites your text.

## Privacy

- Audio never leaves the machine. No cloud STT, no telemetry.
- The correction history is local text only (`~/.local/state/ditado/`), capped at 500 entries.
- Sockets are user-owned (`0600`), systemd units are `--user` scope.

## License

MIT. Model licenses: Whisper models are MIT/Apache-2.0 per ggml-org/whisper.cpp and deepdml/faster-whisper-large-v3-turbo-ct2 (Hugging Face).

---

## Resumo em Português (Brasil)

Ditado por voz **100% local** (nada vai para a nuvem) para o OpenCode Desktop e TUI, com atalho global (F9) que funciona em qualquer janela.

| Placa | Motor | Latência típica (fim da fala → texto) |
|---|---|---|
| AMD/Intel (Vulkan) | whisper.cpp v1.9.5, `large-v3-turbo` f16 | **1,0–1,9 s** (5–30 s de fala) |
| NVIDIA (CUDA) | faster-whisper, `large-v3-turbo` int8 | ~2 s |
| nenhuma | faster-whisper int8 na CPU | ~0,2 s por segundo de áudio |

```bash
sudo apt install cmake build-essential git libvulkan-dev glslc spirv-headers vulkan-tools \
                 alsa-utils ffmpeg xdotool python3-venv
bash install.sh        # detecta a placa e instala (idempotente, sem sudo)
```

- **Dicionário** (`ditado/dicionario.yaml`): corrige termos e nomes próprios na hora e aprende com suas correções (2 ocorrências viram regra). É a peça que mais muda a qualidade — personalize com o seu vocabulário.
- **Cronômetro no botão do mic** (Desktop): `● 00:07 / 03:00`, aviso nos últimos 30 s; limite configurável (`DITADO_MAX_S`, padrão 180 s).
- **Privacidade**: o áudio nunca sai da máquina; sockets e unidades são do usuário (`--user`, 0600).
