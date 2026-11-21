// Ditado por voz no OpenCode Desktop (plugin GUI). F8 ou o botão "Ditar" acima do campo alternam gravar/parar.
// main (Electron/Node) fala com o ditado-stream (/tmp/ditado_stream.sock, JSON por linha: start/stop/cancel/hb/estado);
// a janela recebe parcial/final por Ipc e escreve no campo com composer.setText (sem xdotool).
// Travas: o serviço para sozinho por silêncio, tempo máximo ou falta de hb; aqui há watchdog do final e cancelamento.
import { createEffect, createSignal } from "solid-js"
import { Schema } from "effect"
import * as net from "node:net"
import { Command, ComposerPill, Ipc, Plugin, type ComposerPillView } from "@opencode/plugin/gui"

const SOCK = "/tmp/ditado_stream.sock"

const Fase = Schema.Literals(["ocioso", "gravando", "finalizando", "indisponivel"])

const Mic = Ipc.define({
  id: "ditado-local.mic",
  state: Schema.Struct({ fase: Fase, desde: Schema.Number }),
  methods: {
    alternar: {},
    cancelar: {},
    aprender: { input: Schema.Struct({ entregue: Schema.String, enviado: Schema.String }) },
  },
  events: {
    inicio: Schema.Struct({}),
    parcial: Schema.Struct({ texto: Schema.String }),
    final: Schema.Struct({ texto: Schema.String, ms: Schema.Number }),
    polido: Schema.Struct({ texto: Schema.String }),
    erro: Schema.Struct({ msg: Schema.String }),
  },
})

// Limite de gravação do serviço (DITADO_MAX_S, padrão 180 s).
const LIMITE_S = 180

type FaseT = "ocioso" | "gravando" | "finalizando" | "indisponivel"

// Alucinações do Whisper em trechos de silêncio no fim do ditado (legendas inventadas). Só remove no plugin.
const ALUCINACOES = [
  /\s*legenda(?:\s+[A-ZÀ-Ú][\wÀ-ú]*){1,3}\s*\.?\s*$/gi,
  /\s*jo[ãa]o\s+z[ée]\s*\.?\s*$/gi,
  /amara\.org/gi,
  /obrigad[oa] por assistir[.!]?/gi,
]

const limpa = (t: string) => {
  let s = t
  for (const re of ALUCINACOES) s = s.replace(re, "")
  return s.replace(/\s{2,}/g, " ").trim()
}

const juntar = (base: string, raw: string) => {
  const txt = limpa(raw)
  if (!txt) return base
  if (!base) return txt
  return /\s$/.test(base) ? base + txt : base + " " + txt
}

export default Plugin.define({
  i18n: {
    en: {
      "cmd.ditar": "Dictate (toggle microphone)",
      "pill.idle": "Dictate (F8)",
      "pill.rec": "Recording {t}: click or F8 to stop",
      "pill.fin": "Transcribing…",
      "pill.off": "Dictation unavailable",
      "toast.err": "Dictation",
      "final.vazio": "Nothing recognized; kept the text already written",
    },
    "pt-BR": {
      "cmd.ditar": "Ditar (liga/desliga o microfone)",
      "pill.idle": "Ditar (F8)",
      "pill.rec": "Gravando {t}: clique ou F8 para parar",
      "pill.fin": "Transcrevendo…",
      "pill.off": "Ditado indisponível",
      "toast.err": "Ditado",
      "final.vazio": "Nada reconhecido; mantive o texto já escrito",
    },
    pt: {
      "cmd.ditar": "Ditar (liga/desliga o microfone)",
      "pill.idle": "Ditar (F8)",
      "pill.rec": "Gravando {t}: clique ou F8 para parar",
      "pill.fin": "Transcrevendo…",
      "pill.off": "Ditado indisponível",
      "toast.err": "Ditado",
      "final.vazio": "Nada reconhecido; mantive o texto já escrito",
    },
  },

  main(ctx) {
    let fase = "ocioso" as FaseT
    let desde = 0
    let janela: number | undefined
    let sock: net.Socket | null = null
    let resto = ""
    let hb: ReturnType<typeof setInterval> | undefined
    let wd: ReturnType<typeof setTimeout> | undefined
    let tParou = 0
    let tIni = 0
    let ultimoParcial = ""
    let ultimoDesde = 0

    const provider = ctx.provide(Mic, {
      state: () => ({ fase, desde }),
      alternar: async (_i: unknown, caller: { window: number }) => {
        if (fase === "gravando") return parar()
        if (fase === "finalizando") return
        janela = caller.window
        await iniciar()
      },
      // Camada 3: no Enter, diff entregue × enviado vai ao serviço (candidatos; regra só após 2 ocorrências).
      aprender: async (i: { entregue: string; enviado: string }) => {
        try {
          await conecta()
          envia({ cmd: "aprender", entregue: i.entregue, enviado: i.enviado })
        } catch {}
      },
      cancelar: () => {
        envia({ cmd: "cancel" })
        muda("ocioso")
      },
    })

    function muda(f: FaseT) {
      fase = f
      desde = Date.now()
      if (f === "gravando") {
        clearInterval(hb)
        hb = setInterval(() => envia({ cmd: "hb" }), 3000)
      } else clearInterval(hb)
      if (f !== "finalizando") clearTimeout(wd)
      provider.changed()
    }

    function envia(o: object) {
      try {
        sock?.write(JSON.stringify(o) + "\n")
      } catch {}
    }

    function conecta(): Promise<net.Socket> {
      if (sock && !sock.destroyed) return Promise.resolve(sock)
      return new Promise((res, rej) => {
        const s = net.createConnection(SOCK)
        s.setEncoding("utf8")
        s.once("connect", () => {
          sock = s
          res(s)
        })
        s.once("error", (e) => {
          if (sock !== s) rej(e)
        })
        s.on("data", (d: string) => {
          resto += d
          let i: number
          while ((i = resto.indexOf("\n")) >= 0) {
            const linha = resto.slice(0, i)
            resto = resto.slice(i + 1)
            try {
              recebe(JSON.parse(linha))
            } catch {}
          }
        })
        s.on("close", () => {
          if (sock === s) sock = null
          resto = ""
          if (fase !== "ocioso") {
            provider.emit("erro", { msg: "serviço de ditado desconectou" }, janela)
            muda("ocioso")
          }
        })
      })
    }

    function recebe(m: any) {
      if (m.tipo === "parcial" && fase === "gravando") {
        ultimoParcial = String(m.texto ?? "")
        ultimoDesde = Number(m.desde_inicio_ms ?? 0)
        provider.emit("parcial", { texto: ultimoParcial }, janela)
      } else if (m.tipo === "final") {
        const duracaoAudio = ultimoDesde || Date.now() - tIni
        ctx.log.write("info", "ditado: final recebido", {
          audio_ms: duracaoAudio,
          parar_ate_final_ms: Date.now() - tParou,
          ms_servico: m.ms,
          motivo: m.motivo,
          chars: String(m.texto ?? "").length,
        })
        provider.emit("final", { texto: String(m.texto ?? ""), ms: Number(m.ms ?? 0) }, janela)
        muda("ocioso")
      } else if (m.tipo === "polido") {
        // Camada 2 (LLM local, só correções): chega depois do final; a troca é decidida no lado da janela.
        provider.emit("polido", { texto: String(m.texto ?? "") }, janela)
      } else if (m.tipo === "erro") {
        provider.emit("erro", { msg: String(m.msg ?? "erro") }, janela)
        if (fase !== "gravando" || /arecord|microfone/.test(String(m.msg))) muda("ocioso")
      } else if (m.tipo === "cancelado") muda("ocioso")
    }

    async function iniciar() {
      try {
        await conecta()
      } catch (e) {
        provider.emit("erro", { msg: `serviço de ditado fora (${SOCK}): systemctl --user status ditado-stream` }, janela)
        muda("indisponivel")
        setTimeout(() => fase === "indisponivel" && muda("ocioso"), 4000)
        return
      }
      provider.emit("inicio", {}, janela)
      tIni = Date.now()
      ultimoParcial = ""
      ultimoDesde = 0
      muda("gravando")
      envia({ cmd: "start" })
    }

    function parar() {
      tParou = Date.now()
      ctx.log.write("info", "ditado: parar", { t: tParou })
      muda("finalizando")
      envia({ cmd: "stop" })
      wd = setTimeout(() => {
        if (fase !== "finalizando") return
        envia({ cmd: "cancel" })
        provider.emit("erro", { msg: "o final não chegou em 20 s; ditado cancelado" }, janela)
        muda("ocioso")
      }, 20000)
    }

    ctx.onCleanup(() => {
      clearInterval(hb)
      clearTimeout(wd)
      if (fase !== "ocioso") envia({ cmd: "cancel" })
      sock?.destroy()
    })
    ctx.log.write("info", "ditado-local: main pronto", { sock: SOCK })
  },

  setup(ctx) {
    const mic = ctx.main(Mic)
    let base = ""
    const [agora, setAgora] = createSignal(Date.now())
    const relogio = setInterval(() => {
      if (mic.state()?.fase === "gravando") setAgora(Date.now())
    }, 1000)

    const composer = () => ctx.screen.current()?.composer
    // Texto que o plugin escreveu por último; se o campo esvaziar por fora durante o ditado, a mensagem foi enviada.
    let escrito = ""
    let enviado = false

    const escreve = (t: string) => {
      const c = composer()
      if (!c || enviado) return
      escrito = t
      c.setText(t)
    }

    // Enviar (botão ou Enter) com o microfone ligado: o app limpa o campo; o ditado é cancelado e o que ainda
    // chegar do serviço (parcial ou final atrasado) é descartado, para não reescrever a caixa já enviada.
    createEffect(() => {
      const t = composer()?.text() ?? ""
      const fase = mic.state()?.fase
      if (enviado || !escrito || t !== "") return
      if (fase !== "gravando" && fase !== "finalizando") return
      enviado = true
      escrito = ""
      base = ""
      void mic.cancelar().catch(() => {})
    })

    // Camada 2: o polido só troca o texto se o campo ainda é EXATAMENTE o que o plugin escreveu no final
    // (o usuário não editou nem enviou); senão é descartado.
    let finalEscrito = ""
    let finalBase = ""
    mic.on("polido", (d) => {
      const c = composer()
      if (!c || enviado || !finalEscrito || c.text() !== finalEscrito) return
      const novo = juntar(finalBase, d.texto)
      if (!novo || novo === finalEscrito) return
      finalEscrito = novo
      escrito = novo
      c.setText(novo)
    })

    // Camada 3: acompanha o campo; se esvaziar (Enter/enviar) depois de um ditado entregue, informa o que foi enviado.
    let visto = ""
    createEffect(() => {
      const t = composer()?.text() ?? ""
      if (t !== "") {
        visto = t
        return
      }
      if (visto && finalEscrito) {
        const par = { entregue: finalEscrito, enviado: visto }
        finalEscrito = ""
        visto = ""
        void mic.aprender(par).catch(() => {})
      }
    })

    mic.on("inicio", () => {
      enviado = false
      finalEscrito = ""
      escrito = ""
      base = composer()?.text() ?? ""
    })
    mic.on("parcial", (d) => {
      escreve(juntar(base, d.texto))
    })
    mic.on("final", (d) => {
      const c = composer()
      if (!c || enviado) {
        escrito = ""
        return
      }
      if (d.texto.trim()) {
        escreve(juntar(base, d.texto))
        finalBase = base
        finalEscrito = juntar(base, d.texto)
      } else {
        ctx.toast.show({ variant: "default", title: ctx.t("toast.err"), description: ctx.t("final.vazio"), duration: 4000 })
      }
      c.focus()
      base = ""
      escrito = ""
    })
    mic.on("erro", (d) => {
      ctx.toast.show({ variant: "error", title: ctx.t("toast.err"), description: d.msg, duration: 6000 })
    })

    const alternar = () => {
      void mic.alternar().catch((e: unknown) =>
        ctx.toast.show({ variant: "error", title: ctx.t("toast.err"), description: String(e) }),
      )
    }

    ctx.add(
      Command,
      (): Command => ({
        id: "alternar",
        title: ctx.t("cmd.ditar"),
        keywords: "ditado voz microfone dictation voice mic",
        section: "prompt",
        bind: "f8",
        editable: true,
        run: alternar,
      }),
    )

    ctx.add(ComposerPill, {
      id: "mic",
      order: -10,
      show: (): ComposerPillView => {
        const s = mic.state()
        const fase = s?.fase ?? "ocioso"
        // Rótulo curto "Mic" (a API exige texto), cor por estado; o texto completo fica no tooltip.
        if (fase === "gravando") {
          const seg = Math.max(0, Math.floor((agora() - (s?.desde ?? agora())) / 1000))
          const t = `${String(Math.floor(seg / 60)).padStart(2, "0")}:${String(seg % 60).padStart(2, "0")}`
          // Cronômetro no próprio botão: "● 00:07 / 03:00" (pisca o ponto a cada segundo); o limite é DITADO_MAX_S do serviço (180 s).
          const resta = Math.max(0, LIMITE_S - seg)
          const lim = `${String(Math.floor(LIMITE_S / 60)).padStart(2, "0")}:${String(LIMITE_S % 60).padStart(2, "0")}`
          const ponto = seg % 2 === 0 ? "●" : "○"
          const aviso = resta <= 30 ? " ⚠" : ""
          return { icon: "outline-audio", tone: "danger", label: `${ponto} ${t} / ${lim}${aviso}`, title: ctx.t("pill.rec", { t }), run: alternar }
        }
        if (fase === "finalizando")
          return { icon: "outline-audio", tone: "warning", label: "Transcrevendo…", title: ctx.t("pill.fin") }
        if (fase === "indisponivel")
          return { icon: "outline-audio", tone: "danger", label: "Mic", title: ctx.t("pill.off") }
        return { icon: "outline-audio", label: "Mic", title: ctx.t("pill.idle"), run: alternar }
      },
    })

    return () => clearInterval(relogio)
  },
})
