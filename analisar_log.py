"""
Analisa o ciclos_log.txt e diz se o SLATE roxo ("Commercial break") e culpa do
PROXY (IP) ou da CONTA (token) — pra decidir entre flaggar proxy ou queimar conta.

Uso:
    python analisar_log.py [caminho_do_ciclos_log.txt]

Sem argumento, ele procura o log nos lugares usuais (ao lado do exe / instalado / projeto).
Rode DEPOIS de finalizar a RUN.
"""
import os
import re
import sys
from collections import defaultdict

RE = re.compile(
    r"TOKEN SETADO:\s*(\S+).*?PROXY SETADO:\s*(\S+)", re.IGNORECASE)


def achar_log(argv):
    if len(argv) > 1 and os.path.isfile(argv[1]):
        return argv[1]
    base = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(base, "dist", "MURIADS", "ciclos_log.txt"),      # exe de dev
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "MURIADS", "ciclos_log.txt"),  # instalado
        os.path.join(base, "ciclos_log.txt"),                         # rodando do codigo
        "ciclos_log.txt",
    ]
    existentes = [c for c in cands if c and os.path.isfile(c)]
    if not existentes:
        return None
    # pega o MAIS RECENTE (analisa a ultima RUN, esteja onde estiver)
    return max(existentes, key=os.path.getmtime)


def short(tok, n=10):
    return tok if len(tok) <= n else tok[:n] + "…"


def tabela(titulo, dados, total_por, top=15):
    # dados: {chave: nº de slates}; total_por: {chave: nº total de aparicoes}
    print(f"\n--- {titulo} (top {top} por nº de SLATE) ---")
    print(f"{'SLATE':>5} {'TOTAL':>6} {'TAXA':>6}  IDENTIFICADOR")
    for chave, s in sorted(dados.items(), key=lambda x: -x[1])[:top]:
        tot = total_por.get(chave, s)
        taxa = (100.0 * s / tot) if tot else 0
        print(f"{s:>5} {tot:>6} {taxa:>5.0f}%  {chave}")


def main():
    log = achar_log(sys.argv)
    if not log:
        print("ciclos_log.txt nao encontrado. Passe o caminho:  python analisar_log.py C:\\...\\ciclos_log.txt")
        return
    print(f"Lendo: {log}")
    linhas = open(log, encoding="utf-8", errors="ignore").read().splitlines()

    ciclos = 0
    slate_total = 0
    proxy_total = defaultdict(int); proxy_slate = defaultdict(int)
    token_total = defaultdict(int); token_slate = defaultdict(int)

    for ln in linhas:
        m = RE.search(ln)
        if not m:
            continue
        token, proxy = m.group(1), m.group(2)
        ciclos += 1
        eh_slate = "SLATE" in ln.upper()
        proxy_total[proxy] += 1
        token_total[token] += 1
        if eh_slate:
            slate_total += 1
            proxy_slate[proxy] += 1
            token_slate[token] += 1

    if not ciclos:
        print("Nenhum ciclo com TOKEN/PROXY encontrado no log."); return

    print("\n=== RESUMO ===")
    print(f"Ciclos com identidade (token+proxy): {ciclos}")
    print(f"Ciclos com SLATE roxo: {slate_total}  ({100.0*slate_total/ciclos:.1f}%)")
    if not slate_total:
        print("\nNenhum SLATE registrado — nada a analisar (otimo, ou a RUN foi curta).")
        return

    # offenders = aparecem em SLATE
    proxies_em_slate = len(proxy_slate)
    tokens_em_slate = len(token_slate)
    # 'sempre slate' = 100% de taxa com >=2 aparicoes (reincidente puro)
    proxies_sempre = [p for p, s in proxy_slate.items()
                      if proxy_total[p] >= 2 and s == proxy_total[p]]
    tokens_sempre = [t for t, s in token_slate.items()
                     if token_total[t] >= 2 and s == token_total[t]]

    tabela("POR PROXY (endpoint)", proxy_slate, proxy_total)
    print("\n--- POR CONTA (token) — mostrado parcial ---")
    print(f"{'SLATE':>5} {'TOTAL':>6} {'TAXA':>6}  TOKEN")
    for t, s in sorted(token_slate.items(), key=lambda x: -x[1])[:15]:
        tot = token_total.get(t, s); taxa = 100.0*s/tot if tot else 0
        print(f"{s:>5} {tot:>6} {taxa:>5.0f}%  {short(t)}")

    print("\n=== VEREDITO ===")
    print(f"SLATE atingiu {proxies_em_slate} proxies distintos e {tokens_em_slate} contas distintas.")
    print(f"Proxies que SO deram slate (>=2 aparicoes, 100%): {len(proxies_sempre)}")
    print(f"Contas  que SO deram slate (>=2 aparicoes, 100%): {len(tokens_sempre)}")

    # heuristica simples
    if proxies_sempre and len(proxies_sempre) >= 2 * (len(tokens_sempre) + 1):
        print("\n=> Parece IP-DRIVEN: o slate concentra em proxies reincidentes.")
        print("   Recomendacao: FLAGGAR/descartar o proxy apos N slates (strike de proxy).")
        if proxies_sempre:
            print("   Proxies reincidentes:", ", ".join(proxies_sempre[:10]))
    elif tokens_sempre and len(tokens_sempre) >= 2 * (len(proxies_sempre) + 1):
        print("\n=> Parece CONTA-DRIVEN: o slate concentra em contas reincidentes.")
        print("   Recomendacao: QUEIMAR a conta (nao reusar o token que da slate).")
        if tokens_sempre:
            print("   Contas reincidentes:", ", ".join(short(t) for t in tokens_sempre[:10]))
    else:
        print("\n=> INCONCLUSIVO / MISTO: rode a RUN mais tempo p/ ter reincidencia clara,")
        print("   ou os dois fatores (IP + conta) influenciam. Olhe as taxas acima:")
        print("   proxy/conta com TOTAL>=2 e TAXA=100% sao os 'culpados' confiaveis.")


if __name__ == "__main__":
    main()
