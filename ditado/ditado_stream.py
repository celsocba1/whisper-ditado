#!/usr/bin/env python3
"""Serviço de ditado (prévia opcional) (OpenCode / qualquer cliente).

Socket Unix /tmp/ditado_stream.sock, linhas JSON (uma por linha, UTF-8).
Cliente -> serviço:
  {"cmd":"start"}                 grava o microfone (arecord) e emite parciais
  {"cmd":"stop"}                  para e emite o final (turbo do daemon, com hotwords)
  {"cmd":"cancel"}                descarta
  {"cmd":"file","path":"x.wav"}   teste: injeta WAV 16 kHz em tempo real (como se fosse o mic), depois para sozinho
  {"cmd":"ping"}
  {"cmd":"estado"}               consulta: devolve {"tipo":"estado","ativa":true/false} (não toca no microfone)
Serviço -> cliente:
  {"tipo":"gravando"} | {"tipo":"parcial","texto":...,"ms":N} | {"tipo":"final","texto":...,"ms":N}
  {"tipo":"erro","msg":...} | {"tipo":"pong"} | {"tipo":"cancelado"} | {"tipo":"estado","ativa":bool}

Parciais: modelo `small` int8 (processo próprio, ~0,5 GB de VRAM) sobre janela deslizante com
prefixo confirmado. Final: uma passada do large-v3-turbo no daemon (whisper-ditado.service)
(/tmp/whisper_ditado.sock, protocolo 0x04 com dicas) — o daemon não é alterado.
"""
import glob
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
import base64

AQUI = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.abspath(os.path.join(AQUI, "venv"))
_sp = glob.glob(os.path.join(VENV, "lib", "python3.*", "site-packages"))
# Perfil por máquina: auto (padrão) · cuda · cpu.
DISPOSITIVO_PEDIDO = os.environ.get("DITADO_DISPOSITIVO", "auto").strip().lower() or "auto"
THREADS = int(os.environ.get("DITADO_THREADS", "4"))   # CPU: parciais concorrem com o turbo do daemon
DISPOSITIVO = "cuda"
# Prévia (parciais ao vivo) DESLIGADA por padrão: o texto pronto é o comportamento esperado
# (parcial do `small` escrevia besteira). DITADO_PREVIA=1 religa o loop de parciais e carrega o faster-whisper.
PREVIA = os.environ.get("DITADO_PREVIA", "0").strip().lower() in ("1", "true", "sim", "on")
if PREVIA and _sp and DISPOSITIVO_PEDIDO != "cpu":
    _ld = [os.path.join(_sp[0], "nvidia", d, "lib") for d in ("cublas", "cudnn", "cuda_nvrtc")]
    _ld = [p for p in _ld if os.path.isdir(p)]
    if any(p not in os.environ.get("LD_LIBRARY_PATH", "") for p in _ld):
        os.environ["LD_LIBRARY_PATH"] = ":".join(_ld + [os.environ.get("LD_LIBRARY_PATH", "")])
        py = os.path.join(VENV, "bin", "python3")
        os.execve(py, [py] + sys.argv, os.environ)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dicionario  # noqa: E402
import polir  # noqa: E402
import aprender  # noqa: E402

if PREVIA:
    import numpy as np  # noqa: E402
    from faster_whisper import WhisperModel  # noqa: E402

SOCK = os.environ.get("DITADO_SOCK", "/tmp/ditado_stream.sock")
# Permite apontar para um daemon de teste.
DAEMON_SOCK = os.environ.get("WHISPER_DITADO_SOCK", "").strip() or "/tmp/whisper_ditado.sock"
MODELO_PARCIAL = os.environ.get("DITADO_MODELO_PARCIAL", "small")
HZ = 16000
TETO_S = 300
JANELA_MAX_S = 12.0     # acima disso, confirma o prefixo e desliza a janela
SILENCIO_S = float(os.environ.get("DITADO_SILENCIO_S", "8"))   # para sozinho após N s sem texto novo
MAX_S = float(os.environ.get("DITADO_MAX_S", "180"))             # tempo máximo por ditado
HB_S = float(os.environ.get("DITADO_HB_S", "10"))                # sem sinal do cliente por tanto tempo: cancela
MIN_NOVO_S = 0.4        # só reinfere com pelo menos isso de áudio novo

# Vocabulário das parciais (modo prévia). Personalize com o SEU vocabulário (nomes próprios, jargão).
VOCAB = ("OpenCode Whisper Vulkan CUDA Git Docker Python Claude OpenAI")

_CORR = [
         (r"mikrofone", "microfone"), (r"ride", "RAID"),
         (r"m[ií]cros?\s?t[ií]k", "MikroTik")]
_CORR = [(re.compile(rf"(?<!\w)(?:{p})(?!\w)", re.I), t) for p, t in _CORR]


def corrigir(t):
    """Camada 1 (dicionario.yaml) + reserva da prévia (_CORR, só no modo prévia)."""
    t, _ = dicionario.corrigir(t)
    for rx, r in _CORR:
        t = rx.sub(r, t)
    return t


def log(m):
    sys.stderr.write(f"[ditado_stream {time.strftime('%H:%M:%S')}] {m}\n")
    sys.stderr.flush()


MODELO = None
GPU = threading.Lock()


def carregar():
    global MODELO, DISPOSITIVO
    if not PREVIA:
        DISPOSITIVO = "-"
        return
    erro = None
    if DISPOSITIVO_PEDIDO != "cpu":
        try:
            MODELO = WhisperModel(MODELO_PARCIAL, device="cuda", compute_type="int8")
        except Exception as e:  # noqa: BLE001
            erro = e
            if DISPOSITIVO_PEDIDO == "cuda":
                raise
    if MODELO is None:
        DISPOSITIVO = "cpu"
        if erro:
            log(f"CUDA indisponível ({erro}); parcial em CPU")
        MODELO = WhisperModel(MODELO_PARCIAL, device="cpu", compute_type="int8", cpu_threads=THREADS)
    with GPU:  # aquecimento
        list(MODELO.transcribe(np.zeros(HZ, dtype=np.float32), language="pt")[0])


def inferir(pcm):
    a = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    with GPU:
        segs, _ = MODELO.transcribe(a, language="pt", beam_size=1, vad_filter=True,
                                    vad_parameters=dict(min_silence_duration_ms=300),
                                    condition_on_previous_text=False, hotwords=VOCAB,
                                    without_timestamps=False)
        return [(s.start, s.end, s.text.strip()) for s in segs]


def final_turbo(pcm):
    dica = (dicionario.dicas() or VOCAB).encode()
    corpo = struct.pack(">H", len(dica)) + dica + pcm
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(300)  # ditado de até MAX_S (180 s) + fila; com o daemon na reserva em CPU leva ~0,2 s/s de áudio
    s.connect(DAEMON_SOCK)
    s.sendall(struct.pack(">IB", 1 + len(corpo), 0x04) + corpo)

    def ex(n):
        b = bytearray()
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("daemon fechou")
            b.extend(c)
        return bytes(b)
    r = json.loads(ex(struct.unpack(">I", ex(4))[0]).decode())
    s.close()
    if r.get("erro"):
        raise RuntimeError(r["erro"])
    return r.get("texto", "").strip()


class Sessao:
    def __init__(self, conn):
        self.conn = conn
        self.buf = bytearray()
        self.lock = threading.Lock()   # protege buf
        self.ativa = False
        self.parar = threading.Event()
        self.proc = None
        self.t_ini = 0.0
        self.th_cap = None
        self.th_par = None
        self.wlock = threading.Lock()
        self.trans = threading.Lock()   # serializa finalizar/cancelar
        self.t_hb = time.time()
        self.t_prog = time.time()

    def enviar(self, obj):
        try:
            with self.wlock:
                self.conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        except OSError:
            self.ativa = False
            self.parar.set()

    # --- captura
    def _cap_mic(self):
        try:
            self.proc = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", str(HZ), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as e:
            self.enviar({"tipo": "erro", "msg": f"arecord: {e}"})
            self.parar.set()
            return
        while not self.parar.is_set():
            d = self.proc.stdout.read(3200)
            if not d:
                if not self.parar.is_set():
                    self.enviar({"tipo": "erro", "msg": "microfone fechou"})
                break
            with self.lock:
                self.buf.extend(d)
                if len(self.buf) > TETO_S * HZ * 2:
                    break
        self._matar_proc()

    def _matar_proc(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def _cap_arquivo(self, caminho):
        try:
            with wave.open(caminho, "rb") as w:
                if w.getframerate() != HZ or w.getnchannels() != 1:
                    raise ValueError("WAV precisa ser 16 kHz mono")
                pcm = w.readframes(w.getnframes())
        except Exception as e:  # noqa: BLE001
            self.enviar({"tipo": "erro", "msg": str(e)})
            self.parar.set()
            return
        pcm += b"\0" * (HZ * 2 // 2)  # 0,5 s de silêncio final
        for i in range(0, len(pcm), 3200):
            if self.parar.is_set():
                return
            with self.lock:
                self.buf.extend(pcm[i:i + 3200])
            time.sleep(0.1)
        self.auto_stop = True
        self.parar.set()

    # --- parciais
    def _parciais(self):
        ini = 0           # offset (bytes) da janela
        confirmado = ""
        ultimo_n = 0
        ultimo_txt = None
        while not self.parar.is_set():
            with self.lock:
                n = len(self.buf)
            if n - ultimo_n < MIN_NOVO_S * HZ * 2 or n - ini < 0.3 * HZ * 2:
                time.sleep(0.05)
                continue
            with self.lock:
                janela = bytes(self.buf[ini:n])
            t = time.time()
            try:
                segs = inferir(janela)
            except Exception as e:  # noqa: BLE001
                self.enviar({"tipo": "erro", "msg": f"parcial: {e}"})
                return
            ultimo_n = n
            if self.parar.is_set():
                return
            dur = len(janela) / (HZ * 2)
            if dur > JANELA_MAX_S and len(segs) >= 2:
                corte = segs[-1][0]
                confirmado = (confirmado + " " + " ".join(s[2] for s in segs[:-1])).strip()
                ini += int(corte * HZ) * 2
                segs = segs[-1:]
            txt = corrigir((confirmado + " " + " ".join(s[2] for s in segs)).strip())
            if txt and txt != ultimo_txt:
                ultimo_txt = txt
                self.t_prog = time.time()
                self.enviar({"tipo": "parcial", "texto": txt, "ms": int((time.time() - t) * 1000),
                             "desde_inicio_ms": int((time.time() - self.t_ini) * 1000)})

    def iniciar(self, arquivo=None):
        if self.ativa:
            self.enviar({"tipo": "erro", "msg": "já gravando"})
            return
        self.buf = bytearray()
        self.parar.clear()
        self.auto_stop = False
        self.ativa = True
        self.t_ini = self.t_hb = self.t_prog = time.time()
        threading.Thread(target=self._vigia, daemon=True).start()
        alvo = (lambda: self._cap_arquivo(arquivo)) if arquivo else self._cap_mic
        self.th_cap = threading.Thread(target=alvo, daemon=True)
        self.th_par = threading.Thread(target=self._parciais, daemon=True)
        self.th_cap.start()
        if PREVIA:
            self.th_par.start()
        self.enviar({"tipo": "gravando"})
        if arquivo:
            threading.Thread(target=self._esperar_auto, daemon=True).start()

    def _esperar_auto(self):
        self.parar.wait()
        if self.auto_stop and self.ativa:
            self.finalizar()

    def _vigia(self):
        while self.ativa and not self.parar.is_set():
            time.sleep(0.5)
            agora = time.time()
            if not self.ativa:
                return
            if agora - self.t_hb > HB_S:
                log("cliente sem sinal: cancelando")
                self.cancelar(motivo="cliente_sumiu")
            elif agora - self.t_ini > MAX_S:
                log("tempo máximo")
                self.finalizar(motivo="tempo")
            elif PREVIA and agora - self.t_prog > SILENCIO_S:   # sem prévia não há "texto novo" a medir
                log("silêncio")
                self.finalizar(motivo="silencio")

    def finalizar(self, motivo="parar"):
      with self.trans:
        if not self.ativa:
            return
        self.ativa = False
        self.parar.set()
        self._matar_proc()
        if self.th_cap and self.th_cap is not threading.current_thread():
            self.th_cap.join(timeout=1)  # parciais em voo terminam sozinhos (não bloqueia o final)
        with self.lock:
            pcm = bytes(self.buf)
        t = time.time()
        if len(pcm) < HZ * 2 * 0.3:
            self.enviar({"tipo": "final", "texto": "", "ms": 0, "motivo": motivo})
            return
        try:
            txt = final_turbo(pcm)
        except Exception as e:  # noqa: BLE001
            self.enviar({"tipo": "erro", "msg": f"final (daemon whisper-ditado): {e}"})
            return
        self.entregar_final(txt, t, motivo)

    def entregar_final(self, bruto, t, motivo="parar"):
        """Camadas 0-1 já aplicadas ao Whisper (dicas); aqui a camada 1 (dicionário determinístico)."""
        txt, trocas = dicionario.corrigir(bruto)
        self.enviar({"tipo": "final", "texto": txt, "bruto": bruto, "trocas": trocas,
                     "ms": int((time.time() - t) * 1000), "motivo": motivo})
        if txt:
            aprender.historico_add(txt, bruto)
        if txt and polir.ATIVO:
            threading.Thread(target=self._polir, args=(txt,), daemon=True).start()

    def _polir(self, txt):
        """Camada 2: correções do LLM local (opcional) em segundo plano; o cliente decide se ainda dá para trocar."""
        novo, ok, ms = polir.polir(txt)
        if ok and novo != txt:
            self.enviar({"tipo": "polido", "texto": novo, "base": txt, "trocas": ok, "ms": ms})

    def cancelar(self, motivo="cancelado"):
      with self.trans:
        self.ativa = False
        self.parar.set()
        self._matar_proc()
        self.enviar({"tipo": "cancelado", "motivo": motivo})


def transcrever_pcm(s, m):
    t = time.time()
    try:
        pcm = base64.b64decode(m.get("pcm_b64", ""))
        if len(pcm) < HZ * 2 * 0.3:
            s.enviar({"tipo": "final", "texto": "", "ms": 0, "id": m.get("id")})
            return
        bruto = final_turbo(pcm)
    except Exception as e:  # noqa: BLE001
        s.enviar({"tipo": "erro", "msg": f"texto: {e}", "id": m.get("id")})
        return
    txt, trocas = dicionario.corrigir(bruto)
    s.enviar({"tipo": "final", "texto": txt, "bruto": bruto, "trocas": trocas,
              "ms": int((time.time() - t) * 1000), "id": m.get("id")})
    if txt:
        aprender.historico_add(txt, bruto)


def atender(conn):
    s = Sessao(conn)
    f = conn.makefile("r", encoding="utf-8")
    try:
        for linha in f:
            s.t_hb = time.time()
            try:
                m = json.loads(linha)
            except ValueError:
                continue
            c = m.get("cmd")
            if c == "start":
                s.iniciar()
            elif c == "file":
                s.iniciar(m.get("path"))
            elif c == "stop":
                s.finalizar()
            elif c == "cancel":
                s.cancelar()
            elif c == "aprender":   # Enter: {"cmd":"aprender","entregue":...,"enviado":...}
                try:
                    pr = aprender.registrar(str(m.get("entregue", "")), str(m.get("enviado", "")))
                    if pr:
                        s.enviar({"tipo": "aprendida", "regras": pr})
                except Exception as e:  # noqa: BLE001
                    log(f"aprender: {e}")
            elif c == "texto":   # cliente que grava por conta própria (dsh): {"cmd":"texto","pcm_b64":...}
                threading.Thread(target=transcrever_pcm, args=(s, m), daemon=True).start()
            elif c == "hb":
                pass
            elif c == "ping":
                s.enviar({"tipo": "pong"})
            elif c == "estado":
                s.enviar({"tipo": "estado", "ativa": bool(s.ativa)})
    except Exception as e:  # noqa: BLE001
        log(f"conexão: {e}")
    finally:
        if s.ativa:
            s.cancelar()
        conn.close()


def main():
    carregar()
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)
    signal.signal(signal.SIGTERM, lambda *a: (os.path.exists(SOCK) and os.unlink(SOCK), os._exit(0)))
    log(f"pronto em {SOCK} previa={'on' if PREVIA else 'off'} parcial={MODELO_PARCIAL} dispositivo={DISPOSITIVO} "
        f"threads={THREADS if DISPOSITIVO == 'cpu' else '-'} perfil={DISPOSITIVO_PEDIDO}")
    while True:
        c, _ = srv.accept()
        threading.Thread(target=atender, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
