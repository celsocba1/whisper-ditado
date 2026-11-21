#!/usr/bin/env python3
"""Janela flutuante do ditado global (F9) — terminais, dsh e qualquer campo.

Fluxo: abre e já grava (microfone pelo serviço ditado-stream) -> F9 de novo (ou botão Parar) transcreve ->
mostra o texto PRONTO (camadas 0-1; se vier o polido da camada 2, oferece a troca) -> o usuário revisa/edita ->
Enter (ou botão Colar) copia SEMPRE para a área de transferência e cola na janela que estava em foco.
Esc cancela sem colar (o texto ainda é copiado se já houver). Nada é colado sem confirmação.

Sinais: SIGUSR1 = alterna (parar/gravando) — usado pelo ditar.py no 2º F9.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk

SOCK = os.environ.get("DITADO_SOCK", "/tmp/ditado_stream.sock")
RUN = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "ditado")
PID_ARQ = os.path.join(RUN, "janela.pid")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import aprender
except Exception:  # noqa: BLE001  (fase 3 opcional)
    aprender = None


def xdo(*a):
    try:
        return subprocess.run(["xdotool", *a], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class Janela:
    def __init__(self):
        self.alvo = xdo("getactivewindow")           # janela que estava em foco: recebe a colagem
        self.fase = "conectando"
        self.entregue = ""                            # texto como o serviço entregou (base do aprendizado)
        self.polido = None
        self.editado = False
        self.t0 = time.time()
        self.sock = None
        self.root = tk.Tk()
        r = self.root
        r.title("Ditado")
        r.attributes("-topmost", True)
        r.configure(bg="#1e1e1e")
        self.lbl = tk.Label(r, text="…", fg="#ffffff", bg="#1e1e1e", font=("monospace", 11, "bold"), anchor="w")
        self.lbl.pack(fill="x", padx=8, pady=(6, 2))
        self.txt = tk.Text(r, height=5, width=64, wrap="word", font=("sans", 11), bg="#2b2b2b", fg="#f0f0f0",
                           insertbackground="#ffffff", relief="flat")
        self.txt.pack(fill="both", expand=True, padx=8)
        self.txt.bind("<<Modified>>", self._modificou)
        barra = tk.Frame(r, bg="#1e1e1e")
        barra.pack(fill="x", padx=8, pady=6)
        self.b_main = tk.Button(barra, text="Parar", command=self.principal, width=10)
        self.b_main.pack(side="left")
        self.b_pol = tk.Button(barra, text="Usar polido", command=self.usar_polido, state="disabled")
        self.b_pol.pack(side="left", padx=6)
        tk.Button(barra, text="Cancelar (Esc)", command=self.cancelar).pack(side="right")
        r.bind("<Escape>", lambda e: self.cancelar())
        r.bind("<Control-Return>", lambda e: self.colar())
        r.bind("<Return>", lambda e: (self.colar(), "break")[1] if self.fase == "pronto" else None)
        r.protocol("WM_DELETE_WINDOW", self.cancelar)
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        r.geometry(f"+{(r.winfo_screenwidth() - w) // 2}+{r.winfo_screenheight() - h - 90}")
        signal.signal(signal.SIGUSR1, lambda *a: self.root.after(0, self.principal))
        self.tick()
        threading.Thread(target=self.ouvir, daemon=True).start()

    # --- UI
    def status(self, t, cor="#ffffff"):
        self.lbl.config(text=t, fg=cor)

    def tick(self):
        if self.fase == "gravando":
            s = int(time.time() - self.t0)
            self.status(f"● GRAVANDO {s // 60:02d}:{s % 60:02d}   (F9 ou Parar)", "#ff5555")
        self.root.after(250, self.tick)

    def _modificou(self, _e):
        if self.txt.edit_modified():
            if self.fase == "pronto" and self.txt.get("1.0", "end-1c") != self.entregue:
                self.editado = True
            self.txt.edit_modified(False)

    def set_texto(self, t):
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", t)
        self.txt.edit_modified(False)

    # --- serviço
    def enviar(self, obj):
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode())
        except Exception:  # noqa: BLE001
            pass

    def ouvir(self):
        try:
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(SOCK)
            threading.Thread(target=self.hb, daemon=True).start()
            self.enviar({"cmd": "start"})
            for linha in self.sock.makefile("r", encoding="utf-8"):
                m = json.loads(linha)
                self.root.after(0, self.tratar, m)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self.erro, f"serviço de ditado indisponível ({e})")

    def hb(self):
        while self.sock and self.fase in ("gravando", "transcrevendo", "pronto"):
            self.enviar({"cmd": "hb"})
            time.sleep(3)

    def tratar(self, m):
        if os.environ.get("DITADO_DEBUG"):
            open("/tmp/ditado_janela.log", "a").write(f"{time.time():.1f} {self.fase} {str(m)[:200]}\n")
        t = m.get("tipo")
        if t == "gravando":
            self.fase, self.t0 = "gravando", time.time()
        elif t == "final":
            txt = (m.get("texto") or "").strip()
            self.final_ms = m.get("ms")
            if not txt:
                self.erro("nada reconhecido")
                return
            self.fase, self.entregue = "pronto", txt
            self.set_texto(txt)
            self.status(f"Texto pronto ({(m.get('ms') or 0) / 1000:.1f} s) — Enter cola · Esc cancela", "#7CFC9A")
            self.b_main.config(text="Colar", command=self.colar, state="normal")
            self.txt.focus_set()
        elif t == "polido":   # camada 2: troca sozinha se o usuário ainda não mexeu; senão só oferece
            novo = (m.get("texto") or "").strip()
            if novo and novo != self.entregue:
                if self.fase == "pronto" and not self.editado:
                    self.entregue = novo
                    self.set_texto(novo)
                    self.status("Texto polido aplicado — Enter cola · Esc cancela", "#7CFC9A")
                elif self.fase == "pronto":
                    self.polido = novo
                    self.b_pol.config(state="normal")
        elif t == "erro":
            self.erro(m.get("msg", "erro"))
        elif t == "cancelado":
            self.encerrar()

    def erro(self, msg):
        self.fase = "erro"
        self.status(f"Erro: {msg}", "#ffb86c")
        self.root.after(3500, self.encerrar)

    # --- ações
    def principal(self):
        if self.fase == "gravando":
            self.fase = "transcrevendo"
            self.status("Transcrevendo…", "#ffd866")
            self.b_main.config(state="disabled")
            self.enviar({"cmd": "stop"})
        elif self.fase == "pronto":
            self.colar()

    def usar_polido(self):
        if self.polido:
            self.entregue = self.polido
            self.set_texto(self.polido)
            self.editado = False
            self.b_pol.config(state="disabled")

    def copiar(self, t):
        self.root.clipboard_clear()
        self.root.clipboard_append(t)
        self.root.update()

    def colar(self):
        if self.fase != "pronto":
            return
        t = self.txt.get("1.0", "end-1c").strip()
        if not t:
            return self.cancelar()
        self.copiar(t)                                  # SEMPRE copia
        if aprender:
            try:
                aprender.registrar(self.entregue, t)    # camada 3: diff entregue × confirmado
            except Exception:  # noqa: BLE001
                pass
        self.root.withdraw()
        if self.alvo:
            xdo("windowactivate", "--sync", self.alvo)
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", t], check=False)
        self.fase = "colado"
        self.liberar_pid()
        self.manter_clipboard()

    def manter_clipboard(self):
        """Sem gerenciador de área de transferência o conteúdo morre com o processo: fica vivo até outro app copiar."""
        limite = time.time() + 1800

        def checar():
            try:
                atual = self.root.clipboard_get()
            except tk.TclError:
                atual = None
            if time.time() > limite or (atual is not None and atual != self._ultimo):
                return self.encerrar()
            self.root.after(2000, checar)
        self._ultimo = self.txt.get("1.0", "end-1c").strip()
        self.root.after(2000, checar)

    def cancelar(self):
        if self.fase in ("gravando", "transcrevendo", "conectando"):
            self.enviar({"cmd": "cancel"})
        elif self.fase == "pronto":
            t = self.txt.get("1.0", "end-1c").strip()
            if t:
                self.copiar(t)                          # não cola, mas não perde o texto
                self.fase = "colado"
                self.liberar_pid()
                self.root.withdraw()
                self._ultimo = t
                return self.manter_clipboard()
        self.encerrar()

    def liberar_pid(self):   # novo F9 abre janela nova; este processo só segura a área de transferência
        try:
            os.unlink(PID_ARQ)
        except OSError:
            pass

    def encerrar(self):
        try:
            os.unlink(PID_ARQ)
        except OSError:
            pass
        self.root.destroy()
        os._exit(0)


def main():
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    with open(PID_ARQ, "w") as f:
        f.write(str(os.getpid()))
    Janela().root.mainloop()


if __name__ == "__main__":
    main()
