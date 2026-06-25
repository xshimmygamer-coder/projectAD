"""
Barramento de eventos do orquestrador. Permite servir tanto a CLI (print) quanto a
GUI (fila), sem duplicar logica.

  - emit(tipo, **dados): envia um evento ao sink atual.
  - set_sink(fn): troca o destino (a GUI passa um queue.Queue.put).
  - parar: threading.Event que a GUI seta p/ pedir parada; o orquestrador observa.

Default sink = imprime '@EVT@ {json}' no stdout (modo CLI/headless).
"""
import json
import threading

parar = threading.Event()   # GUI seta -> orquestrador encerra os slots


def _sink_print(ev):
    try:
        print("@EVT@ " + json.dumps(ev, ensure_ascii=False), flush=True)
    except Exception:
        pass


_sink = _sink_print
_arq_lock = threading.Lock()


def sink_arquivo(path):
    """Sink que ANEXA cada evento como 1 JSON por linha em `path`. Usado pela ENGINE
    rodando em processo separado; a GUI faz 'tail' desse arquivo (desacopla UI da engine)."""
    def _s(ev):
        try:
            with _arq_lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return _s


def set_sink(fn):
    """Define o destino dos eventos (callable recebendo um dict). None = volta ao print."""
    global _sink
    _sink = fn or _sink_print


def emit(nome, **dados):
    # 'tipo' = nome do evento; setado por ultimo p/ nunca colidir com um kwarg 'tipo'.
    dados["tipo"] = nome
    try:
        _sink(dados)
    except Exception:
        pass


def reset():
    """Reseta sink e flag (entre RUNs da GUI)."""
    global _sink
    _sink = _sink_print
    parar.clear()
