#!/usr/bin/env python3
"""Ditado global (atalho F9) no desktop Linux.

1º toque: abre a janela flutuante (ditar_janela.py) e grava pelo serviço ditado-stream.
2º toque: para e transcreve (Whisper local, GPU, dicionário); a janela mostra o texto PRONTO e só
cola na janela que estava em foco quando o usuário confirma (Enter); SEMPRE copia para a área de transferência.
Modo antigo (grava e digita direto, sem confirmar): `ditar.py --direto`.

Teste sem microfone: ditar.py --arquivo X.wav --sem-digitar
"""
import argparse
import fcntl
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dicionario  # noqa: E402

SOCKET_PATH = "/tmp/whisper_ditado.sock"
UNIDADE_DAEMON = "whisper-ditado.service"  # systemd --user
TETO_S = 300  # grava no máximo 5 min
RUN = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "ditado")
PID_ARQ = os.path.join(RUN, "arecord.pid")
WAV_ARQ = os.path.join(RUN, "gravacao.wav")
TRAVA_ARQ = os.path.join(RUN, "trava")


def avisar(msg):
    """Notificação na bandeja do desktop (notify-send), sem acumular pilha de avisos."""
    subprocess.run(["notify-send", "-t", "2500", "-h", "string:x-canonical-private-synchronous:ditado",
                    "Ditado", msg], check=False)


def pcm_do_wav(caminho):
    """Extrai PCM s16le 16 kHz mono; converte com ffmpeg se o WAV for de outro formato."""
    import wave
    try:
        with wave.open(caminho, "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2):
                return w.readframes(w.getnframes())
    except wave.Error:
        pass
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", caminho, "-f", "s16le", "-ar", "16000",
                        "-ac", "1", "-"], capture_output=True, check=True)
    return r.stdout


def garantir_daemon():
    """Sobe o daemon (systemd --user) se o socket não responder; espera o modelo carregar."""
    def ping():
        """Tenta o PING 0x02 no socket do daemon."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(SOCKET_PATH)
            s.sendall(struct.pack(">IB", 1, 0x02))
            ok = len(s.recv(4)) == 4
            s.close()
            return ok
        except OSError:
            return False
    if ping():
        return True
    avisar("Iniciando o daemon de voz (carrega o modelo)...")
    subprocess.run(["systemctl", "--user", "start", UNIDADE_DAEMON], check=False)
    for _ in range(60):
        if ping():
            return True
        time.sleep(1)
    return False


def transcrever(pcm):
    """Manda o PCM ao daemon com as dicas do dicionário e devolve o texto corrigido."""
    dica = dicionario.dicas().encode()  # camada 0: termos do dicionário como vocabulário
    corpo = struct.pack(">H", len(dica)) + dica + pcm  # comando 0x04: u16 n + dicas + PCM
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(180)
    s.connect(SOCKET_PATH)
    s.sendall(struct.pack(">IB", 1 + len(corpo), 0x04) + corpo)

    def exato(n):
        """Lê exatamente n bytes do socket (o daemon responde em quadros)."""
        b = bytearray()
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("daemon fechou a conexão")
            b.extend(c)
        return bytes(b)
    tam = struct.unpack(">I", exato(4))[0]
    resp = json.loads(exato(tam).decode("utf-8"))
    s.close()
    if resp.get("erro"):
        raise RuntimeError(resp["erro"])
    return dicionario.corrigir(resp.get("texto", "").strip())[0]   # camada 1


def digitar(texto):
    """Digita o texto no campo focado com xdotool."""
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", texto], check=False)


def iniciar_gravacao():
    """Abre o arecord em sessão própria e guarda o PID; o teto de gravação é do próprio arecord."""
    # microfone padrão do PulseAudio/PipeWire; timeout do arecord = teto de gravação
    p = subprocess.Popen(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
                          "-d", str(TETO_S), "-t", "wav", WAV_ARQ],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.3)
    if p.poll() is not None:
        avisar("Falha ao abrir o microfone (arecord)")
        return
    with open(PID_ARQ, "w") as f:
        f.write(str(p.pid))
    avisar("Gravando... (F9 para parar)")


def parar_e_transcrever(pid, digitar_texto):
    """Fecha o arecord, espera o WAV ficar íntegro e manda transcrever."""
    try:
        os.kill(pid, signal.SIGINT)  # SIGINT fecha o cabeçalho do WAV
    except ProcessLookupError:
        pass
    for _ in range(30):  # espera o arecord sair
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            break
    try:
        os.unlink(PID_ARQ)
    except OSError:
        pass
    try:
        if not os.path.exists(WAV_ARQ):
            avisar("Sem áudio gravado")
            return
        avisar("Transcrevendo...")
        if not garantir_daemon():
            avisar("Daemon de voz indisponível (systemctl --user status whisper-ditado)")
            return
        texto = transcrever(pcm_do_wav(WAV_ARQ))
        _entregar(texto, digitar_texto)
    except Exception as e:  # noqa: BLE001
        avisar(f"Erro: {e}")
    finally:
        try:
            os.unlink(WAV_ARQ)
        except OSError:
            pass


def _entregar(texto, digitar_texto):
    """Entrega o texto: imprime na saída ou digita no campo, com aviso na bandeja."""
    if not texto:
        avisar("Nada reconhecido")
        return
    if digitar_texto:
        avisar(texto[:80])
        digitar(texto)
    else:
        print(texto)


def alternar_janela():
    """F9: sem janela gravando -> abre uma; com janela -> SIGUSR1 (parar e transcrever)."""
    pidf = os.path.join(RUN, "janela.pid")
    try:
        pid = int(open(pidf).read().strip())
        os.kill(pid, 0)
        os.kill(pid, signal.SIGUSR1)
        return
    except (OSError, ValueError):
        pass
    subprocess.Popen(["/usr/bin/python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ditar_janela.py")],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def pid_vivo():
    """Devolve o PID do arecord se ele ainda estiver de pé."""
    try:
        pid = int(open(PID_ARQ).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def main():
    """Linha de comando: --arquivo para teste, --direto para o modo antigo; o resto é o F9 alternando."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--arquivo", help="transcreve este WAV em vez de gravar (teste)")
    ap.add_argument("--direto", action="store_true", help="modo antigo: grava e digita sem a janela de confirmação")
    ap.add_argument("--sem-digitar", action="store_true", help="imprime o texto em vez de digitar")
    a = ap.parse_args()

    if a.arquivo:
        if not garantir_daemon():
            sys.exit("daemon de voz indisponível")
        t0 = time.time()
        texto = transcrever(pcm_do_wav(a.arquivo))
        print(f"[{time.time() - t0:.2f}s]", file=sys.stderr)
        _entregar(texto, not a.sem_digitar)
        return

    os.makedirs(RUN, mode=0o700, exist_ok=True)
    if not a.direto:
        return alternar_janela()
    trava = open(TRAVA_ARQ, "w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)  # toque rápido demais: ignora
    except OSError:
        return
    pid = pid_vivo()
    if pid:
        parar_e_transcrever(pid, not a.sem_digitar)
    else:
        try:
            os.unlink(PID_ARQ)
        except OSError:
            pass
        iniciar_gravacao()


if __name__ == "__main__":
    main()
