#!/usr/bin/env python3
"""Daemon Whisper local compartilhado (socket Unix, protocolo v2).

Motores, escolhidos por WHISPER_DITADO_DISPOSITIVO:
  auto   (padrão) tenta CUDA (faster-whisper em GPU NVIDIA); sem CUDA usa o perfil vulkan (ex.: notebook com Radeon 780M).
  cuda   faster-whisper na GPU NVIDIA; se falhar, o serviço falha alto.
  cpu    faster-whisper int8 na CPU, modelo carregado no boot.
  vulkan cliente HTTP do whisper-server (whisper.cpp + Vulkan, unit whisper-vulkan.service em 127.0.0.1:47111).
         Servidor fora → reserva: o MESMO turbo do faster-whisper em CPU, carregado sob demanda.
Os pedidos entram em fila (um por vez no motor); o PING responde na hora, mesmo com transcrição em curso.
"""
import contextlib
import glob
import heapq
import importlib.util
import io
import json
import math
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import wave

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "venv"))
site_packages_dirs = glob.glob(os.path.join(VENV_DIR, "lib", "python3.*", "site-packages"))
SITE_PACKAGES = site_packages_dirs[0] if site_packages_dirs else ""

# Perfil de execução: auto (padrão) · cuda · cpu · vulkan.
# Nenhum perfil degrada o modelo: é sempre o large-v3-turbo (faster-whisper ou ggml f16 no whisper.cpp).
DISPOSITIVO_PEDIDO = os.environ.get("WHISPER_DITADO_DISPOSITIVO", "auto").strip().lower() or "auto"
if DISPOSITIVO_PEDIDO not in ("auto", "cuda", "cpu", "vulkan"):
    sys.stderr.write(f"[whisper_daemon] WHISPER_DITADO_DISPOSITIVO inválido: {DISPOSITIVO_PEDIDO!r}\n")
    sys.exit(2)

# Libs CUDA do pip (nvidia/*): só se existirem no venv e o perfil puder usar CUDA (auto ou cuda).
if SITE_PACKAGES and DISPOSITIVO_PEDIDO in ("auto", "cuda"):
    cublas_lib = os.path.join(SITE_PACKAGES, "nvidia", "cublas", "lib")
    cudnn_lib = os.path.join(SITE_PACKAGES, "nvidia", "cudnn", "lib")
    nvrtc_lib = os.path.join(SITE_PACKAGES, "nvidia", "cuda_nvrtc", "lib")
    ld_paths = [p for p in [cublas_lib, cudnn_lib, nvrtc_lib] if os.path.isdir(p)]

    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    precisa_reexec = False
    for p in ld_paths:
        if p not in current_ld:
            precisa_reexec = True
            break

    if precisa_reexec:
        novo_ld = ":".join(ld_paths) + (":" + current_ld if current_ld else "")
        os.environ["LD_LIBRARY_PATH"] = novo_ld
        venv_python = os.path.join(VENV_DIR, "bin", "python3")
        py_exe = venv_python if os.path.exists(venv_python) else sys.executable
        os.execve(py_exe, [py_exe] + sys.argv, os.environ)

try:
    import numpy as np
    if importlib.util.find_spec("faster_whisper") is None:  # importado sob demanda (_fw), mas exigido no venv
        raise ImportError("faster_whisper ausente")
except ImportError:
    venv_python = os.path.join(VENV_DIR, "bin", "python3")
    if os.path.exists(venv_python) and sys.executable != venv_python:
        os.execve(venv_python, [venv_python] + sys.argv, os.environ)
    raise

# Protocolo (socket Unix, quadros big-endian): u32 tamanho (= 1 + dados) | u8 comando | dados.
#   0x02 PING     → {"status","versao","modelo","dispositivo","threads","motor","modelo_motor","servidor","reserva","fila"}
#                   ("modelo" é o id do faster-whisper; o cliente pode comparar com o dele e reiniciar o daemon)
#   0x03 STOP     → encerra (o cliente manda ao fechar: libera a VRAM)
#   0x01 PCM      → PCM s16le 16 kHz mono (legado, sem dicas)
#   0x04 PCM+DICA → u16 n | n bytes UTF-8 de dicas (glossário) | PCM s16le 16 kHz mono
#   0x07 PCM+DICA+ID → u32 id | u16 n | dicas | PCM  (14/11/2026; id devolve no JSON e permite cancelar)
#   0x05 CANCELAR → u32 id | resposta {"cancelado": bool, "id", "saiu_da_fila"}
#   0x08 PCM+DICA+ID FINAL → igual ao 0x07, mas com PRIORIDADE no motor (fura parcial) — 14/11/2026
#   0x09 PCM+DICA+CTX → u32 id | u16 nd | u16 nc | dicas | contexto | pcm — contexto do bloco anterior
#                      no prompt do ASR (D0-5, 14/11/2026): é o que impede o bloco de ser "no escuro"
#   0x06 CALIBRAR → mede o backend real do whisper-server e devolve {"backend","ms","motor"}; recusa na fila
#                   do motor (usa TRAVA_MOTOR). Nunca no PING: a calibração custa uma inferência inteira.
# 0x02 devolve também "backend" e "calibracao_ms" (backend_cache, sem inferência) — desde 14/11/2026.
# Resposta: u32 tamanho | JSON {"texto","ms","motor"} ou {"erro"}.
VERSAO = 5
SOCKET_PATH = os.environ.get("WHISPER_DITADO_SOCK", "").strip() or "/tmp/whisper_ditado.sock"
HZ = 16000
# Medido em bateria de testes: turbo int8 + hotwords em lote dá WER natural 3,1 % (small: 11,2 %)
# e não pula trecho no ditado longo.
MODELO = os.environ.get("WHISPER_DITADO_MODELO", "").strip() or "deepdml/faster-whisper-large-v3-turbo-ct2"
LOTE = int(os.environ.get("WHISPER_DITADO_LOTE", "4"))
BEAM = int(os.environ.get("WHISPER_DITADO_BEAM", "2"))
# CPU: 8 threads mediu melhor que 16 (contenção). Na GPU não se aplica.
THREADS = int(os.environ.get("WHISPER_DITADO_THREADS", "8"))
DISPOSITIVO = "cuda"

# Perfil vulkan: whisper-server residente da unit whisper-vulkan.service. Porta fixa 47111.
# O modo de decodificação (f16, -fa, -bs 2, -l pt) é fixado na unit, não aqui.
SERVIDOR = (os.environ.get("WHISPER_DITADO_SERVIDOR", "").strip() or "http://127.0.0.1:47111").rstrip("/")
MODELO_VULKAN = os.environ.get("WHISPER_DITADO_MODELO_VULKAN", "").strip() or "ggml-large-v3-turbo-f16"
ESPERA_SERVIDOR_S = float(os.environ.get("WHISPER_DITADO_ESPERA_SERVIDOR", "15"))  # só no boot
# Orçamento do prompt (revisão de 14/11/2026 — whisper.cpp v1.9.5):
#   max_prompt_ctx = min(n_max_text_ctx, n_text_ctx/2) = 224 tokens para o large-v3 (whisper.cpp:7101);
#   com carry_initial_prompt=false (default do server) a truncagem é `end() - n_take1`, ou seja COME O
#   COMEÇO e mantém o FIM, e é SILENCIOSA (o WARN só existe no ramo carry_initial_prompt=true).
#   800 caracteres de pt-BR ≈ 230-260 tokens → o prompt de hoje já perde os primeiros termos, e como
#   DICAS_BASE vem primeiro, quem sumia era o glossário. 650 chars ≈ 190-210 tokens: folga
#   para nunca truncar no servidor. Confirmar com o tokenizador antes de subir este número.
PROMPT_MAX = 650
# Contexto entre blocos (D0-5, 14/11/2026): implementado e DESLIGADO por padrão.
# Medido com o corpus sintético: sem contexto o juntado ficou com WER 0,429 contra a passada única;
# com contexto de 40 palavras subiu para 0,833, e com 6 palavras para 0,881 — o decoder reemite o
# prompt (falha clássica do initial_prompt). Só ligar depois de medir com a voz real do usuário.
CONTEXTO_ATIVO = os.environ.get("WHISPER_DITADO_CONTEXTO", "0").strip() in ("1", "true", "sim")

MOTOR = ""            # "faster-whisper-cuda" | "faster-whisper-cpu" | "whispercpp-vulkan"
MODELO_FW = None      # WhisperModel do faster-whisper (cuda/cpu, ou a reserva do perfil vulkan)
TRAVA_MOTOR = threading.Lock()   # mantido por compatibilidade; a ordem real agora é FILA_MOTOR (14/11/2026)


class FilaPrioridade:
    """Fila explícita com prioridade: final (0) fura parcial (1); calibração (2) por último.

    Por que não `threading.Lock`: ele não é FIFO nem prioriza — acorda quem quiser, então uma parcial
    disparada a cada 1,5 s conseguia entrar na frente do pedido final. Aqui o próximo a rodar é
    sempre o de menor número (e, empatado, o que chegou antes).
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._heap = []      # [(prioridade, seq, evento)]
        self._seq = 0
        self._ocupado = False
        self._atual = None

    @contextlib.contextmanager
    def vez(self, prioridade=1, ev=None):
        # `ev` vem do PEDIDOS do pedido (14/11/2026): a fila NÃO pode criar um Event próprio, senão o
        # cancelamento por id procura um objeto que nunca está no heap e nunca cancela nada.
        ev = ev or threading.Event()
        with self._cv:
            self._seq += 1
            heapq.heappush(self._heap, (prioridade, self._seq, ev))
            while self._ocupado or self._heap[0][2] is not ev:
                self._cv.wait()
            heapq.heappop(self._heap)
            self._ocupado = True
            self._atual = ev
        try:
            yield
        finally:
            with self._cv:
                self._ocupado = False
                self._atual = None
                self._cv.notify_all()

    def cancelar(self, ev):
        """Estado do cancelamento, sob a MESMA Condition que faz fila→rodando (sem janela de corrida).

        "retirado"   = saiu do heap e nunca vai rodar
        "ja_rodando" = já pegou o motor; o custo foi pago e a resposta será abandonada
        "desconhecido" = não é desta fila
        """
        with self._cv:
            for i, (_, _, e) in enumerate(self._heap):
                if e is ev:
                    self._heap.pop(i)
                    heapq.heapify(self._heap)
                    self._cv.notify_all()
                    return "retirado"
            return "ja_rodando" if ev is self._atual else "desconhecido"


FILA_MOTOR = FilaPrioridade()
TRAVA_RESERVA = threading.Lock()
TRAVA_FILA = threading.Lock()
FILA = 0              # pedidos de transcrição em curso ou aguardando (vai no PING)
_SAUDE = {"t": 0.0, "ok": None}  # cache da saúde do servidor (o PING espera só 800 ms)
# Backend REAL do servidor Vulkan (14/11/2026): o whisper.cpp sobe mesmo sem GPU e cai para CPU em silêncio
# ("no GPU found") — o motor continua dizendo whispercpp-vulkan e todo número de latência passa a mentir.
# A classificação é por TEMPO (o encode domina): ~1,3 s na 780M × ~8,1 s em CPU por janela de 30 s.
# Calculada no boot e sob demanda (comando 0x06), nunca no PING: o PING tem 800 ms e não pode pagar inferência.
_CALIBRACAO = {"t": 0.0, "backend": None, "ms": None, "motivo": ""}
# Pedidos com id (14/11/2026): sem id não há como cancelar um pedido que já entrou na fila do motor.
# O cliente manda PCM com id (0x07), cancela a parcial que não interessa mais (0x05) e o pedido é
# descartado antes de pegar TRAVA_MOTOR — é o que impede a parcial de ficar na frente do final.
PEDIDOS = {}                 # id -> threading.Event (setado = cancelado)
TRAVA_PEDIDOS = threading.Lock()


def log(msg):
    sys.stderr.write(f"[whisper_daemon {time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def _fw():
    """Importa o faster-whisper só quando é usado.

    No perfil vulkan o daemon pode passar o dia sem tocar no CTranslate2; importar sob demanda poupa
    ~200 MB de RAM e o tempo de boot. Devolve o módulo faster_whisper.
    """
    import faster_whisper  # noqa: PLC0415
    return faster_whisper


def _carregar_cpu():
    global DISPOSITIVO, LOTE
    DISPOSITIVO = "cpu"
    LOTE = 1  # BatchedInferencePipeline só compensa na GPU
    log(f"carregando {MODELO} em CPU int8 ({THREADS} threads)...")
    return _fw().WhisperModel(MODELO, device="cpu", compute_type="int8", cpu_threads=THREADS)


# ---------------------------------------------------------------------------------------------------------------
# servidor_ok(forcar=False, timeout=0.3) -> bool
#   Consulta GET /health do whisper-server e diz se ele está pronto (HTTP 200, modelo carregado).
#   O resultado fica em cache por 2 s: o PING espera só 800 ms e não pode pagar uma
#   consulta HTTP a cada chamada. Registra no log a passagem ok→fora e fora→ok (uma linha por transição).
#   forcar=True ignora o cache (usado no boot e antes de cada transcrição).
# ---------------------------------------------------------------------------------------------------------------
def servidor_ok(forcar=False, timeout=0.3):
    agora = time.time()
    if not forcar and _SAUDE["ok"] is not None and agora - _SAUDE["t"] < 2.0:
        return _SAUDE["ok"]
    try:
        with urllib.request.urlopen(SERVIDOR + "/health", timeout=timeout) as r:
            ok = r.status == 200 and b"ok" in r.read(200)
    except (OSError, urllib.error.URLError, ValueError):
        ok = False
    if _SAUDE["ok"] is not None and ok != _SAUDE["ok"]:
        log(f"servidor Vulkan {SERVIDOR}: {'voltou (ok)' if ok else 'FORA — pedidos vão para a reserva em CPU'}")
    _SAUDE.update(t=agora, ok=ok)
    return ok


def _marcar_fora():
    """Invalida o cache de saúde depois de uma falha real no /inference (a próxima consulta refaz o GET)."""
    if _SAUDE["ok"]:
        log(f"servidor Vulkan {SERVIDOR}: FORA (falha no /inference)")
    _SAUDE.update(t=time.time(), ok=False)


def _modo_vulkan(espera_s):
    """Entra no perfil vulkan: não carrega modelo nenhum, só espera o servidor até `espera_s` segundos.

    A unit whisper-vulkan.service é Type=simple, então o whisper-ditado pode nascer antes de o modelo ggml
    terminar de carregar (~1–2 s). Se o servidor não responder no prazo, o daemon sobe assim mesmo e a
    reserva em CPU é carregada no primeiro pedido.
    """
    global DISPOSITIVO, MOTOR
    DISPOSITIVO, MOTOR = "vulkan", "whispercpp-vulkan"
    limite = time.time() + espera_s
    while not servidor_ok(forcar=True) and time.time() < limite:
        time.sleep(0.5)
    if not _SAUDE["ok"]:
        log(f"servidor Vulkan {SERVIDOR} fora no boot; reserva {MODELO} em CPU será carregada sob demanda")


def carregar_modelo():
    """Escolhe o motor pelo perfil. Mesmo modelo em qualquer dispositivo; se não carregar, falha alto (sem cair para base)."""
    global MODELO_FW, MOTOR
    if DISPOSITIVO_PEDIDO == "cpu":
        MODELO_FW, MOTOR = _carregar_cpu(), "faster-whisper-cpu"
        return
    if DISPOSITIVO_PEDIDO == "vulkan":
        _modo_vulkan(ESPERA_SERVIDOR_S)
        return
    erro_cuda = None
    try:  # sem GPU, falha em ~0,1 s ("CUDA driver version is insufficient")
        log(f"carregando {MODELO} em CUDA int8...")
        MODELO_FW = _fw().WhisperModel(MODELO, device="cuda", compute_type="int8")
        MOTOR = "faster-whisper-cuda"
        log("modelo na GPU")
        return
    except Exception as e:  # noqa: BLE001
        erro_cuda = e
    if DISPOSITIVO_PEDIDO == "cuda":
        raise RuntimeError(f"perfil cuda pedido e CUDA falhou: {erro_cuda}")
    # auto sem CUDA: tenta o servidor Vulkan (espera curta); fora → reserva em CPU sob demanda.
    log(f"CUDA indisponível ({erro_cuda}); tentando o servidor Vulkan {SERVIDOR}")
    _modo_vulkan(min(ESPERA_SERVIDOR_S, 5.0))


def _reserva_cpu():
    """Devolve o turbo do faster-whisper em CPU, carregando-o na primeira vez (perfil vulkan com servidor fora).

    Fica residente depois de carregado (~1,1 GB de RAM) até o daemon reiniciar: se o servidor cair uma vez,
    é provável que caia de novo, e recarregar custaria ~1,5 s a cada queda.
    """
    global MODELO_FW
    with TRAVA_RESERVA:
        if MODELO_FW is None:
            log(f"servidor Vulkan fora: carregando a reserva {MODELO} em CPU sob demanda")
            t = time.time()
            MODELO_FW = _fw().WhisperModel(MODELO, device="cpu", compute_type="int8", cpu_threads=THREADS)
            log(f"reserva em CPU carregada em {time.time() - t:.1f} s")
        return MODELO_FW


def recv_exact(conn, n):
    data = bytearray()
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def responder(conn, obj):
    corpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    conn.sendall(struct.pack(">I", len(corpo)) + corpo)


PIPELINE = None

# Dicas fixas do daemon (sempre entram no prompt). Personalize com o SEU vocabulário — nomes próprios
# e jargão específico são o que o Whisper mais erra.
DICAS_BASE = ("OpenCode Whisper Vulkan CUDA Git Docker Python systemd "
              "OpenAI Claude local")

# Erros típicos do ditado (edite para o seu vocabulário): palavra inteira, sem diferenciar maiúscula nem acento.
_CORRECOES = [
    (r"ride", "RAID"),
]
_CORRECOES_RE = [(re.compile(rf"(?<!\w)(?:{p})(?!\w)", re.IGNORECASE), t) for p, t in _CORRECOES]
# Marcas de não-fala do whisper.cpp ("[BLANK_AUDIO]", "[ Silêncio ]", "*Risos*", "*BANG*"): não são texto ditado.
_MARCAS_RE = re.compile(r"\[[^\]]{0,40}\]|\*[^*\n]{1,30}\*")


def corrigir_nomes(texto):
    for rx, troca in _CORRECOES_RE:
        base = "".join(unicodedata.normalize("NFD", c)[0] for c in texto)
        if len(base) != len(texto):
            base = texto
        saida, pos = [], 0
        for m in rx.finditer(base):
            saida += [texto[pos:m.start()], troca]
            pos = m.end()
        texto = "".join(saida) + texto[pos:]
    return texto


def _transcrever_fw(model, pcm, dicas):
    """Transcrição pelo faster-whisper (cuda, cpu ou a reserva do perfil vulkan). Mesmo código de antes do F7."""
    audio = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    global PIPELINE
    kw = dict(language="pt", beam_size=BEAM, vad_filter=True, vad_parameters=dict(min_silence_duration_ms=300))
    dicas = (dicas + " " + DICAS_BASE).strip()
    if dicas:
        kw["hotwords"] = dicas  # vale em todas as janelas; initial_prompt alucinou no turbo
    if DISPOSITIVO == "cuda" and LOTE > 1:
        if PIPELINE is None:
            PIPELINE = _fw().BatchedInferencePipeline(model=model)
        segmentos, _info = PIPELINE.transcribe(audio, batch_size=LOTE, **kw)
    else:
        segmentos, _info = model.transcribe(audio, condition_on_previous_text=False, **kw)
    return corrigir_nomes(" ".join(s.text.strip() for s in segmentos).strip())


# ---------------------------------------------------------------------------------------------------------------
# _wav_em_memoria(pcm) -> bytes
#   Embrulha o PCM s16le 16 kHz mono do protocolo num WAV completo, só na memória (sem arquivo temporário),
#   que é o formato que o whisper-server aceita sem --convert/ffmpeg.
# ---------------------------------------------------------------------------------------------------------------
def _wav_em_memoria(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(HZ)
        w.writeframes(pcm[: len(pcm) - len(pcm) % 2])
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------------------------
# _multipart(campos, arquivo_wav) -> (corpo, content_type)
#   Monta o multipart/form-data do POST /inference sem depender de requests: um campo "file" com o WAV e
#   um campo por parâmetro (response_format, temperature, language, prompt).
# ---------------------------------------------------------------------------------------------------------------
def _multipart(campos, arquivo_wav):
    fronteira = "voz" + uuid.uuid4().hex
    partes = []
    for nome, valor in campos.items():
        partes.append(f"--{fronteira}\r\nContent-Disposition: form-data; name=\"{nome}\"\r\n\r\n".encode()
                      + str(valor).encode("utf-8") + b"\r\n")
    partes.append(f"--{fronteira}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"ditado.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n".encode() + arquivo_wav + b"\r\n")
    partes.append(f"--{fronteira}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={fronteira}"


# ---------------------------------------------------------------------------------------------------------------
# _prompt_glossario(dicas) -> str
#   Monta o prompt inicial do whisper.cpp a partir de DICAS_BASE + dicas do cliente (espaço, vírgula ou ponto e
#   vírgula), sem repetir termo (sem diferenciar maiúscula), no formato "Glossário: a, b, c." e com no máximo
#   PROMPT_MAX caracteres (os termos que não couberem saem do fim; as DICAS_BASE vêm primeiro).
#   Por que esse formato (medido com turbo f16 -fa -bs 2; lista crua de palavras e sem prompt também medidos):
#     lista crua de palavras: WER 13,7 % nas frases e 19,6 % no longo — o modelo imita o prompt sem
#     pontuação e embaralha o texto; sem prompt: 12,3 % / 6,8 %; "Glossário: a, b." : 6,8 % / 2,7 %.
# ---------------------------------------------------------------------------------------------------------------
def _prompt_com_contexto(dicas, contexto):
    """Prompt do ASR com CONTEXTO + glossário (D0-5, 14/11/2026).

    Ordem importa: o whisper.cpp trunca pelo **começo** e mantém o fim, então o glossário — que é o
    que mediu ganho de WER — vai por último, e o contexto (texto do bloco anterior) vem antes.
    O contexto é cortado em fronteira de termo e nunca no meio de palavra.
    """
    gloss = _prompt_glossario(dicas)
    ctx = " ".join((contexto or "").split())
    if not ctx or not CONTEXTO_ATIVO:
        return gloss
    limite = PROMPT_MAX - len(gloss) - 2
    if limite < 40:  # sem espaço para contexto útil: o glossário manda
        return gloss
    if len(ctx) > limite:
        ctx = ctx[-limite:]
        corte = ctx.find(" ")
        ctx = ctx[corte + 1:] if corte >= 0 else ctx
    return (ctx + " " + gloss)[:PROMPT_MAX]


def _prompt_glossario(dicas):
    termos, vistos, tamanho = [], set(), len("Glossário: .")
    for t in re.split(r"[\s,;]+", DICAS_BASE + " " + (dicas or "")):
        t = t.strip(" .:")
        if not t or t.lower() in vistos:
            continue
        if tamanho + len(t) + 2 > PROMPT_MAX:
            break
        vistos.add(t.lower())
        termos.append(t)
        tamanho += len(t) + 2
    return ("Glossário: " + ", ".join(termos) + ".") if termos else ""


# ---------------------------------------------------------------------------------------------------------------
# _transcrever_servidor(pcm, dicas) -> str
#   Envia o áudio ao whisper-server (POST /inference) e devolve o texto já corrigido.
#   - prompt = glossário (DICAS_BASE + dicas do cliente) em frase pontuada (_prompt_glossario); o whisper.cpp
#     não tem hotwords, o prompt inicial é o equivalente;
#   - temperature 0.0 (o servidor ainda sobe a temperatura sozinho se a decodificação falhar: temperature_inc);
#   - timeout proporcional ao áudio: 30 s + 0,5 s por segundo de áudio (5 min de ditado → 180 s; uma iGPU
#     faz ~1,2 s por janela de 30 s, então o prazo só estoura com o servidor travado);
#   - marcas de não-fala ("[BLANK_AUDIO]") são removidas; espaços e quebras de linha entre segmentos colapsados.
#   Qualquer falha (conexão, HTTP, JSON, "error") vira exceção: quem chama decide cair para a reserva.
# ---------------------------------------------------------------------------------------------------------------
def _transcrever_servidor(pcm, dicas, contexto=""):
    prompt = _prompt_com_contexto(dicas, contexto)
    campos = {"response_format": "json", "temperature": "0.0", "language": "pt"}
    if prompt:
        campos["prompt"] = prompt
    corpo, tipo = _multipart(campos, _wav_em_memoria(pcm))
    duracao_s = len(pcm) / (2 * HZ)
    req = urllib.request.Request(SERVIDOR + "/inference", data=corpo, method="POST",
                                 headers={"Content-Type": tipo})
    with urllib.request.urlopen(req, timeout=30 + 0.5 * duracao_s) as r:
        resp = json.loads(r.read().decode("utf-8", "replace"))
    if "error" in resp:
        raise RuntimeError(f"whisper-server: {resp['error']}")
    texto = _MARCAS_RE.sub(" ", resp.get("text", ""))
    return corrigir_nomes(" ".join(texto.split()))


# ---------------------------------------------------------------------------------------------------------------
# _pcm_calibracao(segundos) -> bytes
#   Áudio sintético só para medir o backend (nunca áudio do usuário). O Whisper normaliza a janela para 30 s, então
#   o custo do encode é o mesmo com 2 s ou com 10 s: o que muda é o backend, e é isso que se quer medir.
# ---------------------------------------------------------------------------------------------------------------
def _pcm_calibracao(segundos=2.0):
    n = int(HZ * segundos)
    amostras = []
    for i in range(n):
        t = i / HZ
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * t)  # modulação de sílaba
        v = 700 * env * (math.sin(2 * math.pi * 180 * t) + 0.5 * math.sin(2 * math.pi * 320 * t))
        amostras.append(int(max(-32000, min(32000, v))))
    return struct.pack("<%dh" % n, *amostras)


def backend_cache():
    """Backend conhecido, SEM inferência (seguro para o PING de 800 ms)."""
    if MOTOR != "whispercpp-vulkan":
        return {"backend": "cuda" if MOTOR == "faster-whisper-cuda" else "cpu", "calibracao_ms": None}
    return {"backend": _CALIBRACAO["backend"] or "nao_medido", "calibracao_ms": _CALIBRACAO["ms"]}


def calibrar_backend(forcar=False):
    """Mede o backend real do whisper-server pelo tempo de uma inferência de calibração."""
    if MOTOR != "whispercpp-vulkan":
        backend = "cuda" if MOTOR == "faster-whisper-cuda" else "cpu"
        _CALIBRACAO.update(t=time.time(), backend=backend, ms=None, motivo=f"motor {MOTOR}")
        return dict(_CALIBRACAO, motor=MOTOR)
    if not forcar and _CALIBRACAO["backend"] and (time.time() - _CALIBRACAO["t"]) < 600:
        return dict(_CALIBRACAO, motor=MOTOR)
    try:
        corpo, tipo = _multipart({"response_format": "json", "temperature": "0.0", "language": "pt"},
                                 _wav_em_memoria(_pcm_calibracao()))
        req = urllib.request.Request(SERVIDOR + "/inference", data=corpo, method="POST",
                                     headers={"Content-Type": tipo})
        t0 = time.time()
        # A calibração paga uma inferência inteira: sem este lock ela corria junto com um pedido real,
        # media fila em vez de backend (falso `lento`/`cpu`) e ainda atrasava o primeiro ditado.
        with FILA_MOTOR.vez(2):  # calibração nunca fura ditado
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        ms = int((time.time() - t0) * 1000)
        # 2 s de áudio: ≤3 s = GPU (medido 1,5 s); >6 s = CPU (medido ~8 s na janela cheia).
        backend = "vulkan" if ms <= 3000 else ("lento" if ms <= 6000 else "cpu")
        _CALIBRACAO.update(t=time.time(), backend=backend, ms=ms, motivo="")
        log(f"backend real: {backend} ({ms} ms na calibração de 2 s)")
        if backend != "vulkan":
            log("ATENÇÃO: whisper-server sem GPU — o ditado fica ~5x mais lento. Ver /dev/dri e os grupos render/video.")
    except Exception as e:  # noqa: BLE001
        _CALIBRACAO.update(t=time.time(), backend="desconhecido", ms=None, motivo=str(e))
        log(f"calibração do backend falhou: {e}")
    return dict(_CALIBRACAO, motor=MOTOR)


# ---------------------------------------------------------------------------------------------------------------
# _tem_fala(pcm) -> bool
#   Porta de energia do perfil vulkan (o faster-whisper tem vad_filter; o whisper-server, sem VAD, alucina no
#   silêncio). Mede o RMS em quadros de 30 ms e só considera fala se o quadro mais forte passar de 4× o piso
#   (percentil 10: as pausas entre sílabas; há dinâmica, fala sobre ruído) e de 150 em s16 (~ -47 dBFS).
#   Medido em 07/11/2026: 3 s de zeros → "E aí" em 4,9 s; 5 s de ruído baixo → "Sous-titrage: DimaTorzok".
#   O VAD silero do próprio servidor (--vad) resolvia o silêncio mas subiu o WER do áudio longo de 2,7 % para
#   15,5 % e somou ~0,2 s por frase, por isso a porta fica aqui.
# ---------------------------------------------------------------------------------------------------------------
def _tem_fala(pcm):
    a = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32)
    q = int(0.03 * HZ)
    n = len(a) // q
    if n < 3:
        return True  # curto demais para medir: deixa o motor decidir
    rms = np.sqrt(np.mean(a[: n * q].reshape(n, q) ** 2, axis=1))
    pico, piso = float(rms.max()), float(np.percentile(rms, 10))
    # Revisão de 07/11: com AGC do microfone ou ruído de fundo o piso sobe e a razão 4× pode
    # recusar fala real. Pico forte (>= 1200, ~ -29 dBFS) passa sempre; a razão só filtra o sinal fraco.
    # WHISPER_PORTA_ENERGIA=0 desliga a porta (diagnóstico).
    if os.environ.get("WHISPER_PORTA_ENERGIA", "1") == "0":
        return True
    return pico >= 150.0 and (pico >= 1200.0 or pico >= 4.0 * max(piso, 1.0))


# ---------------------------------------------------------------------------------------------------------------
# transcrever(pcm, dicas) -> (texto, motor_usado)
#   Ponto único de transcrição, chamado com TRAVA_MOTOR já tomada (fila).
#   Perfil vulkan: servidor pronto → whisper.cpp; servidor fora ou falha no /inference → reserva em CPU
#   (carregada sob demanda). Demais perfis: faster-whisper como antes.
# ---------------------------------------------------------------------------------------------------------------
def transcrever(pcm, dicas, contexto=""):
    if len(pcm) < 3200:  # < 0,1 s
        return "", MOTOR
    if MOTOR == "whispercpp-vulkan":
        if not _tem_fala(pcm):
            return "", MOTOR
        if servidor_ok(forcar=True, timeout=1.0):
            try:
                return _transcrever_servidor(pcm, dicas, contexto), MOTOR
            except Exception as e:  # noqa: BLE001
                log(f"falha no whisper-server ({e}); este pedido vai para a reserva em CPU")
                _marcar_fora()
        log("pedido atendido pela reserva em CPU (servidor Vulkan fora)")
        return _transcrever_fw(_reserva_cpu(), pcm, dicas), "faster-whisper-cpu (reserva)"
    return _transcrever_fw(MODELO_FW, pcm, dicas), MOTOR


def saude():
    """Resposta do PING (0x02): estado do daemon e do motor, sem esperar a fila (o cliente dá só 800 ms)."""
    r = {"status": "ok", "versao": VERSAO, "modelo": MODELO, "dispositivo": DISPOSITIVO,
         "threads": THREADS if DISPOSITIVO == "cpu" else None, "motor": MOTOR, "fila": FILA}
    r.update(backend_cache())  # backend REAL ("vulkan" | "lento" | "cpu" | "nao_medido") — 14/11/2026
    if MOTOR == "whispercpp-vulkan":
        r.update(modelo_motor=MODELO_VULKAN, servidor="ok" if servidor_ok() else "fora",
                 reserva="carregada" if MODELO_FW is not None else "sob demanda")
    return r


def ja_ativo():
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCKET_PATH)
        s.sendall(struct.pack(">IB", 1, 0x02))
        ok = len(s.recv(4)) == 4
        s.close()
        return ok
    except Exception:  # noqa: BLE001
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------------------------------------------
# atender(conn, sair)
#   Atende uma conexão do socket (uma thread por conexão). PING e STOP respondem na hora; PCM entra na fila
#   do motor (TRAVA_MOTOR), de modo que dois clientes (ditado_stream, plugin…) nunca transcrevem ao mesmo
#   tempo e o PING continua respondendo enquanto um ditado longo é transcrito.
# ---------------------------------------------------------------------------------------------------------------
def atender(conn, sair):
    global FILA
    try:
        while True:
            cab = recv_exact(conn, 5)
            if not cab:
                break
            tamanho, cmd = struct.unpack(">IB", cab)
            if tamanho > 128 * 1024 * 1024:  # > ~65 min de PCM 16 kHz: quadro inválido, não aloca (revisão 07/11)
                break
            dados =recv_exact(conn, tamanho - 1) if tamanho > 1 else b""
            if dados is None:
                break
            if cmd == 0x02:
                responder(conn, saude())
            elif cmd == 0x03:
                responder(conn, {"status": "encerrando"})
                conn.close()
                sair()
            elif cmd == 0x06:  # CALIBRAR: mede o backend real agora (pedido manual; 14/11/2026)
                responder(conn, calibrar_backend(forcar=True))
            elif cmd == 0x05:  # CANCELAR: u32 id (14/11/2026)
                rid = struct.unpack(">I", dados[:4])[0] if len(dados) >= 4 else 0
                with TRAVA_PEDIDOS:
                    ev = PEDIDOS.get(rid)
                if ev is None:
                    responder(conn, {"cancelado": False, "id": rid, "estado": "desconhecido"})
                else:
                    ev.set()
                    estado = FILA_MOTOR.cancelar(ev)
                    responder(conn, {"cancelado": estado == "retirado", "id": rid, "estado": estado})
            elif cmd in (0x01, 0x04, 0x07, 0x08, 0x09):
                dicas = ""
                pcm = dados
                rid = None
                if cmd in (0x07, 0x08, 0x09) and len(dados) >= 4:  # u32 id | ...
                    rid = struct.unpack(">I", dados[:4])[0]
                    dados = dados[4:]
                contexto = ""
                if cmd == 0x09 and len(dados) >= 4:  # u32 id (já extraído) | u16 nd | u16 nc | dicas | ctx | pcm
                    nd = struct.unpack(">H", dados[0:2])[0]
                    nc = struct.unpack(">H", dados[2:4])[0]
                    dicas = dados[4:4 + nd].decode("utf-8", "ignore")
                    contexto = dados[4 + nd:4 + nd + nc].decode("utf-8", "ignore")
                    pcm = dados[4 + nd + nc:]
                elif cmd in (0x04, 0x07, 0x08) and len(dados) >= 2:
                    n = struct.unpack(">H", dados[:2])[0]
                    dicas = dados[2:2 + n].decode("utf-8", "ignore")
                    pcm = dados[2 + n:]
                ev = None
                if rid is not None:
                    ev = threading.Event()
                    with TRAVA_PEDIDOS:
                        PEDIDOS[rid] = ev
                with TRAVA_FILA:
                    FILA += 1
                try:
                    prioridade = 0 if cmd == 0x08 else 1
                    with FILA_MOTOR.vez(prioridade, ev):
                        if ev is not None and ev.is_set():  # cancelado enquanto esperava a fila
                            responder(conn, {"texto": "", "ms": 0, "motor": MOTOR, "cancelado": True})
                            continue
                        t = time.time()
                        texto, motor = transcrever(pcm, dicas, contexto)
                    extra = {"id": rid} if rid is not None else {}
                    responder(conn, {"texto": texto, "ms": int((time.time() - t) * 1000), "motor": motor, **extra})
                except Exception as e:  # noqa: BLE001
                    responder(conn, {"erro": str(e), "texto": ""})
                finally:
                    with TRAVA_FILA:
                        FILA -= 1
                    if rid is not None:
                        with TRAVA_PEDIDOS:
                            PEDIDOS.pop(rid, None)
            else:
                responder(conn, {"erro": f"comando desconhecido {cmd}"})
    except Exception as e:  # noqa: BLE001
        log(f"erro na conexão: {e}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    if ja_ativo():
        log("já há daemon ativo no socket; saindo")
        return
    try:
        carregar_modelo()
    except Exception as e:  # noqa: BLE001 — falha alta: o systemd registra e reinicia
        log(f"FALHA ao carregar {MODELO} (perfil={DISPOSITIVO_PEDIDO}): {e}")
        sys.exit(1)
    antigo = os.umask(0o177)  # socket nasce 600: só o mesmo usuário fala com ele
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.umask(antigo)
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(8)

    def sair(*_):
        try:
            server.close()
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
        finally:
            os._exit(0)

    signal.signal(signal.SIGINT, sair)
    signal.signal(signal.SIGTERM, sair)
    if MOTOR == "whispercpp-vulkan":
        log(f"pronto em {SOCKET_PATH} (v{VERSAO}) motor={MOTOR} modelo={MODELO_VULKAN} "
            f"servidor={'ok' if _SAUDE['ok'] else 'fora'} ({SERVIDOR}) reserva={MODELO} (CPU sob demanda) "
            f"perfil={DISPOSITIVO_PEDIDO}")
    else:
        log(f"pronto em {SOCKET_PATH} (v{VERSAO}) motor={MOTOR} dispositivo={DISPOSITIVO} modelo={MODELO} "
            f"threads={THREADS if DISPOSITIVO == 'cpu' else '-'} lote={LOTE} perfil={DISPOSITIVO_PEDIDO}")

    # Calibração de backend no boot (14/11/2026): em segundo plano, para o socket já estar aceitando.
    # Se a GPU não estiver disponível, o PING mostra backend="cpu" e o app avisa em vez de mentir.
    if MOTOR == "whispercpp-vulkan":
        threading.Thread(target=calibrar_backend, kwargs={"forcar": True}, daemon=True).start()

    while True:
        try:
            conn, _ = server.accept()
        except Exception:  # noqa: BLE001
            break
        threading.Thread(target=atender, args=(conn, sair), daemon=True).start()


if __name__ == "__main__":
    main()
