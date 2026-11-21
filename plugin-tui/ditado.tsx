/** @jsxImportSource @opentui/solid */
// Ditado por voz no OpenCode (TUI). F8 alterna gravar/parar; /ditar-parar é a parada de emergência.
// Serviço: ditado-stream (systemd --user). Socket Unix /tmp/ditado_stream.sock, JSON por linha.
// LED do mic ao lado do prompt (slots home_prompt_right e session_prompt_right): verde ● MIC ON,
// cinza ● mic off, vermelho ◌ mic indisponível (serviço fora; checagem a cada 15 s).
// Travas: o serviço para sozinho por silêncio, tempo máximo ou cliente sem heartbeat; aqui há ainda
// watchdog de "transcrevendo" e parada de emergência. Log de diagnóstico: ~/.local/state/ditado-plugin.log
import { createSignal, createMemo } from "solid-js"
import * as net from "node:net"
import { appendFileSync, statSync, truncateSync, mkdirSync } from "node:fs"
import { exec } from "node:child_process"
import { homedir } from "node:os"

const SOCK = process.env.DITADO_SOCK ?? "/tmp/ditado_stream.sock"
const TECLA = process.env.DITADO_TECLA ?? "f8"
const LOG = process.env.DITADO_DEBUG ?? `${homedir()}/.local/state/ditado-plugin.log`

function dbg(...a: unknown[]) {
  try {
    mkdirSync(`${homedir()}/.local/state`, { recursive: true })
    try {
      if (statSync(LOG).size > 200_000) truncateSync(LOG, 0)
    } catch {}
    appendFileSync(LOG, new Date().toISOString() + " " + a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ") + "\n")
  } catch {}
}

// P2: captura o TuiPromptRef via props dos slots (home_prompt/session_prompt) e usa ref.set/current
// em vez de varrer o renderer. Fallback achaPrompt permanece só para o prompt da tela home.
let promptRef: any = null
let fallbackUsado = false

// Acha a caixa de texto do prompt (foco primeiro, depois varredura da árvore).
function achaPrompt(renderer: any): any | null {
  const ok = (n: any) => n && typeof n.plainText === "string" && typeof n.setText === "function"
  const f = renderer?.currentFocusedRenderable
  if (ok(f)) return f
  const vistos = new Set<any>()
  const dfs = (n: any, d: number): any => {
    if (!n || d > 40 || vistos.has(n)) return null
    vistos.add(n)
    if (ok(n)) return n
    const fs = typeof n.getChildren === "function" ? n.getChildren() : (n._children ?? [])
    for (const c of fs ?? []) {
      const r = dfs(c, d + 1)
      if (r) return r
    }
    return null
  }
  return dfs(renderer?.root, 0)
}

const tui = async (api: any) => {
  type Est = "ocioso" | "gravando" | "finalizando"
  const [est, setEst] = createSignal<Est>("ocioso")
  const [seg, setSeg] = createSignal(0)
  let sock: net.Socket | null = null
  let base = ""
  let ta: any = null
  let vivo = false
  let ultimoParcial = ""
  let tToast = 0
  let tIni = 0
  let tStop = 0
  let hb: any = null
  let tick: any = null
  let wd: any = null
  let tUltTecla = 0
  // Enter com mic ON (22/10): intercepta o submit, para a captura e
  // espera o "final" com timeout curto antes de enviar a mensagem.
  let resolveSubmit: ((txt: string | null) => void) | null = null
  // Trava da submissão (bug da duplicação no Enter, 22/10): do início do submitDitado até
  // o próximo ditado, NENHUMA escrita no prompt — parcial/final/estado tardios são ignorados.
  let enviando = false

  function esperaFinal(ms: number): Promise<string | null> {
    return new Promise((res) => {
      resolveSubmit = res
      setTimeout(() => {
        if (resolveSubmit === res) {
          resolveSubmit = null
          res(null)
        }
      }, ms)
    })
  }

  // Reconciliação do indicador (bug do MIC ON preso, 22/10): com o indicador ativo,
  // consulta o estado real do serviço a cada 3 s; se divergir, corrige e registra no log.
  let resolveEstado: ((ok: boolean) => void) | null = null
  let esperandoEstado = false
  function reconciliaEst() {
    if (est() === "ocioso" || esperandoEstado || enviando) return
    esperandoEstado = true
    const devolve = (ok: boolean) => {
      esperandoEstado = false
      if (!ok) {
        dbg("reconcilia; serviço diz que NÃO está gravando mas indicador sim; corrigindo para ocioso")
        zera()
      }
    }
    resolveEstado = devolve
    envia({ cmd: "estado" })
    setTimeout(() => {
      if (resolveEstado === devolve) {
        resolveEstado = null
        devolve(false)
      }
    }, 1200)
  }
  setInterval(reconciliaEst, 3000)

  async function submitDitado(onSubmit: () => void) {
    if (enviando) {
      dbg("submitDitado; submit já em curso, Enter ignorado")
      return
    }
    enviando = true
    let escrito = ""
    try {
      if (est() === "gravando") {
        setEst("finalizando")
        tStop = Date.now()
        clearTimeout(wd)
        envia({ cmd: "stop" })
      }
      let txt = await esperaFinal(2500)
      if (!txt) txt = ultimoParcial.trim()
      if (txt) {
        if (!escrever(txt)) dbg("submitDitado; falha escrevendo texto final")
      } else {
        toast("warning", "Ditado", "nada reconhecido; enviando sem ditado", 2500)
      }
      // Descarta o resto do áudio para nada vazar no próximo ditado.
      envia({ cmd: "cancel" })
      zera()
      vivo = false
      base = ""
      ultimoParcial = ""
      // Marca o que foi escrito no prompt para a checagem pós-submit.
      try {
        escrito = String(promptRef?.current?.input ?? "")
      } catch {}
    } catch (e) {
      dbg("submitDitado erro", String(e))
      zera()
      vivo = false
      base = ""
      ultimoParcial = ""
    }
    // Submete UMA vez. Enquanto enviando=true, trata() ignora parcial/final/estado —
    // nenhuma escrita no prompt depois daqui (bug da duplicação no Enter, 22/10).
    let enviou = false
    try {
      onSubmit()
      enviou = true
    } catch (e) {
      dbg("submitDitado; on_submit falhou", String(e))
    }
    // Garantia "caixa vazia": se o host não limpou o texto do ditado, limpa agora
    // (só se o resíduo for exatamente o que foi escrito; texto digitado pelo usuário é preservado).
    if (enviou && escrito.trim()) {
      try {
        const restou = String(promptRef?.current?.input ?? "")
        if (restou === escrito) {
          const cur = promptRef.current ?? {}
          promptRef.set({ input: "", mode: cur.mode, parts: cur.parts ?? [] })
          dbg("submitDitado; texto ditado restou após submit; limpando")
        }
      } catch (e) {
        dbg("submitDitado; checagem pós-submit falhou", String(e))
      }
    }
  }

  const toast = (variant: string, title: string, message: string, duration = 2500) =>
    api.ui.toast({ variant, title, message, duration })

  function escrever(texto: string) {
    try {
      const sep = base && !/\s$/.test(base) && texto ? " " : ""
      const novo = base + sep + texto
      if (promptRef) {
        const cur = promptRef.current ?? {}
        promptRef.set({ input: novo, mode: cur.mode, parts: cur.parts ?? [] })
        return true
      }
      if (!ta) return false
      ta.setText(novo)
      if (typeof ta.gotoBufferEnd === "function") ta.gotoBufferEnd()
      ta.requestRender?.()
      return true
    } catch (e) {
      dbg("erro escrever", String(e))
      if (!promptRef) ta = null
      return false
    }
  }

  function zera() {
    setEst("ocioso")
    clearInterval(hb)
    clearInterval(tick)
    clearTimeout(wd)
    try {
      sock?.destroy()
    } catch {}
    sock = null
  }

  function envia(o: object) {
    try {
      sock?.write(JSON.stringify(o) + "\n")
    } catch (e) {
      dbg("erro envia", String(e))
    }
  }

  function trata(m: any) {
    if (m.tipo !== "parcial") dbg("msg", m)
    if (m.tipo === "gravando") {
      toast("info", "● Gravando", `${TECLA.toUpperCase()} para parar`, 3000)
    } else if (m.tipo === "parcial") {
      if (enviando) return
      ultimoParcial = m.texto
      if (est() !== "gravando") return
      if (escrever(m.texto)) vivo = true
      else if (Date.now() - tToast > 400) {
        tToast = Date.now()
        toast("info", "● Gravando", m.texto.length > 160 ? "…" + m.texto.slice(-160) : m.texto)
      }
    } else if (m.tipo === "final") {
      if (resolveSubmit) {
        const r = resolveSubmit
        resolveSubmit = null
        r((m.texto ?? "").trim())
        return
      }
      if (enviando) {
        dbg("final ignorado (submit em curso / já entregue)")
        return
      }
      const txt = (m.texto ?? "").trim()
      const porque = m.motivo === "silencio" ? "parou por silêncio" : m.motivo === "tempo" ? "parou: tempo máximo" : ""
      // Estado limpo ANTES de escrever/toast: qualquer final (vazio, cancelado, erro)
      // leva o indicador a "mic off" mesmo se algo falhar depois (bug do MIC ON preso).
      zera()
      if (!txt) toast("warning", "Ditado", porque || "Nada reconhecido")
      else if (vivo && escrever(txt)) toast("success", "Ditado", porque || `pronto (${m.ms} ms)`, 2000)
      else {
        Promise.resolve(api.client.tui.appendPrompt({ text: (base ? " " : "") + txt })).catch(() => {})
        toast("success", "Ditado", porque || `pronto (${m.ms} ms)`, 2000)
      }
    } else if (m.tipo === "estado") {
      const r = resolveEstado
      resolveEstado = null
      r?.(!!m.ativa)
    } else if (m.tipo === "erro") {
      toast("error", "Ditado", String(m.msg), 6000)
      if (resolveSubmit) {
        const r = resolveSubmit
        resolveSubmit = null
        r(null)
      }
      zera()
    } else if (m.tipo === "cancelado") {
      if (m.motivo === "cliente_sumiu") toast("warning", "Ditado", "cancelado (sem sinal do cliente)")
      if (resolveSubmit) {
        const r = resolveSubmit
        resolveSubmit = null
        r(null)
      }
      zera()
    }
  }

  function inicia() {
    enviando = false
    if (promptRef) {
      try {
        base = String(promptRef.current?.input ?? "")
        dbg("inicia; promptRef ok, base len:", base.length)
      } catch (e) {
        dbg("inicia; promptRef.current falhou:", String(e))
        base = ""
      }
    } else {
      ta = achaPrompt(api.renderer)
      base = ta ? String(ta.plainText ?? "") : ""
      if (ta) {
        fallbackUsado = true
        dbg("AVISO: sem TuiPromptRef, usando fallback achaPrompt (frágil)")
      }
    }
    vivo = false
    ultimoParcial = ""
    const s = net.createConnection(SOCK)
    sock = s
    setEst("gravando")
    tIni = Date.now()
    setSeg(0)
    tick = setInterval(() => setSeg(Math.floor((Date.now() - tIni) / 1000)), 1000)
    hb = setInterval(() => envia({ cmd: "hb" }), 3000)
    let buf = ""
    s.setEncoding("utf8")
    s.on("connect", () =>
      s.write(JSON.stringify(process.env.DITADO_ARQUIVO ? { cmd: "file", path: process.env.DITADO_ARQUIVO } : { cmd: "start" }) + "\n"),
    )
    s.on("data", (d: string) => {
      buf += d
      let i: number
      while ((i = buf.indexOf("\n")) >= 0) {
        const l = buf.slice(0, i)
        buf = buf.slice(i + 1)
        try {
          trata(JSON.parse(l))
        } catch (e) {
          dbg("json", String(e))
        }
      }
    })
    s.on("error", (e: any) => {
      setDisp("fora")
      toast("error", "Ditado fora do ar", `serviço ditado-stream não responde (${e.code ?? e.message}). systemctl --user start ditado-stream`, 6000)
      zera()
    })
    s.on("close", () => {
      if (sock === s && est() !== "ocioso") {
        toast("warning", "Ditado", "conexão com o serviço caiu; mic desligado")
        zera()
      }
    })
  }

  // Emergência: descarta tudo, fecha o socket; se o serviço não confirmar em 2 s, reinicia o serviço (mata o arecord).
  function emergencia(motivo: string) {
    dbg("emergencia", motivo)
    envia({ cmd: "cancel" })
    const antigo = sock
    zera()
    toast("warning", "Ditado", "mic desligado (parada de emergência)", 3000)
    setTimeout(() => {
      try {
        antigo?.destroy()
      } catch {}
    }, 300)
    exec("systemctl --user is-active ditado-stream >/dev/null && sleep 2 && pgrep -f 'arecord.*-t raw' >/dev/null && systemctl --user restart ditado-stream", () => {})
  }

  function alterna() {
    // Debounce: ignora o segundo toque em menos de ~300 ms (bug do MIC ON preso, 22/10).
    const agora = Date.now()
    if (agora - tUltTecla < 300) {
      dbg("alterna; ignorado (debounce <300 ms)")
      return
    }
    tUltTecla = agora
    dbg("alterna; estado:", est())
    if (est() === "ocioso") return inicia()
    if (est() === "gravando") {
      setEst("finalizando")
      tStop = Date.now()
      toast("info", "Transcrevendo…", ultimoParcial ? ultimoParcial.slice(-120) : "", 5000)
      envia({ cmd: "stop" })
      wd = setTimeout(() => est() === "finalizando" && emergencia("watchdog finalizando"), 20000)
      return
    }
    // finalizando: segundo toque depois de 3 s = desistir
    if (Date.now() - tStop > 3000) emergencia("F8 durante finalização")
  }

  // LED do microfone (22/10): verde = ativo, cinza = inativo, vermelho ◌ = indisponível.
  const [disp, setDisp] = createSignal<"ok" | "fora">("ok")
  function checaServico() {
    const t = net.createConnection(SOCK)
    t.setTimeout(800)
    t.on("connect", () => {
      try {
        t.destroy()
      } catch {}
      setDisp("ok")
    })
    t.on("error", () => setDisp("fora"))
    t.on("timeout", () => {
      try {
        t.destroy()
      } catch {}
      setDisp("fora")
    })
  }
  checaServico()
  const chk = setInterval(checaServico, 15000)

  const mm = (n: number) => `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`
  // Reativo de verdade (bug do indicador preso, 22/10): em Solid o corpo do componente roda UMA vez,
  // então os ramos if do Ind antigo nunca reagiam a setEst. Aqui um memo calcula cor+texto e o <text>
  // atualiza por atributo/conteúdo reativo — o slot reage a inicia/transcrevendo/zera.
  const Ind = () => {
    const st = createMemo(() => {
      if (disp() === "fora") return { fg: "#ff3b20", t: "◌ mic indisponível" }
      if (est() === "gravando") return { fg: "#22c55e", t: `● MIC ON ${mm(seg())}` }
      if (est() === "finalizando") return { fg: "#ffa500", t: "… transcrevendo" }
      return { fg: "#707070", t: "● mic off" }
    })
    return <text fg={st().fg}>{st().t}</text>
  }
  // Captura o TuiPromptRef: recria o Prompt da sessão (props conhecidos: session_id, visible,
  // disabled, on_submit) com ref próprio que repassa ao host, preservando o comportamento nativo.
  const [Prompt, Slot] = [api.ui.Prompt, api.ui.Slot]
  const SessaoPrompt = (props: any) => (
    <Prompt
      sessionID={props.session_id}
      visible={props.visible}
      disabled={props.disabled}
      onSubmit={() => {
        // Mic ON/FINALIZANDO: Enter desliga o mic, aguarda o texto final e envia.
        if (enviando) {
          dbg("onSubmit; Enter ignorado (submissão em curso)")
          return
        }
        if (est() === "ocioso") return props.on_submit?.()
        void submitDitado(() => props.on_submit?.())
      }}
      ref={(r: any) => {
        promptRef = r
        try {
          props.ref?.(r)
        } catch (e) {
          dbg("erro repassando ref ao host", String(e))
        }
      }}
      right={<Slot name="session_prompt_right" session_id={props.session_id} />}
    />
  )
  api.slots.register({ slots: { session_prompt: (p: any) => <SessaoPrompt {...p} />, home_prompt_right: () => <Ind />, session_prompt_right: () => <Ind /> } })

  const off = api.command.register(() => [
    {
      title: "Ditar (gravar/parar)",
      value: "ditado.alternar",
      description: "Ditado por voz: parcial ao vivo e final corrigido",
      category: "Ditado",
      keybind: TECLA,
      slash: { name: "ditar" },
      onSelect: alterna,
    },
    {
      title: "Ditar: parar (emergência)",
      value: "ditado.parar",
      description: "Desliga o microfone e descarta o ditado",
      category: "Ditado",
      slash: { name: "ditar-parar", aliases: ["ditarparar", "parar-ditado"] },
      onSelect: () => emergencia("comando"),
    },
  ])
  api.lifecycle?.onDispose?.(() => {
    clearInterval(chk)
    off()
    envia({ cmd: "cancel" })
    zera()
  })
  dbg("plugin carregado")
}

export default { id: "ditado", tui }
