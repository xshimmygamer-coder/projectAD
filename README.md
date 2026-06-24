# twitch_swap — Etapa 1: rotacao de contas/proxies/fingerprint (AdsPower, CDP-free)

Roda N perfis AdsPower em paralelo (1 thread por perfil). Cada perfil (slot)
fica em loop: pega uma conta + um proxy livres da pool, gera um fingerprint
aleatorio coerente, **limpa o cache** do perfil (cookie/localStorage/indexeddb),
seta cookie (`auth-token`) + proxy + fingerprint num so `user/update`, abre na
home da Twitch, espera a sessao (10-15 min, configuravel), fecha e devolve a
conta + proxy pra pool. Assim poucas contas ON ao mesmo tempo (= nº de perfis),
revezando uma pool maior de contas.

## Por que limpa o cache
O `update cookie` so grava no metadata do perfil. O `auth-token`/sessao antigos
ficam no cookie store + localStorage em disco, e o Twitch le os antigos -> o
login NAO troca. Limpar `cookie`+`local_storage`+`indexeddb` (endpoint
`/api/v2/browser-profile/delete-cache`, perfil fechado) e a peca que faz o swap
funcionar. Validado ao vivo.

## Setup
1. `pip install requests playwright pywin32`  (pywin32 = TaskView ao vivo)
2. Edite `swap.py`:
   - `API_KEY` = sua API key da Local API do AdsPower.
   - `SESSAO_MIN_S` / `SESSAO_MAX_S` = duracao da sessao por conta.
3. Preencha os dados:
   - `tokens.txt` = seus cookies: 1 `auth-token` por linha (cole o grupo aqui).
     Opcional `apelido,token`; '#'=comentario; vazias e duplicados ignorados.
     -> ao rodar, o `swap.py` gera o `contas_pool.json` automaticamente a partir daqui.
   - `proxies_pool.txt` = SOCKS5 (`host:port` ou `host:port:user:senha`), 1/linha.
     Tenha pelo menos 1 proxy por perfil (senao slots esperam proxy livre).
   - Perfis: detectados automaticamente via AdsPower API (/api/v1/user/list). Nao ha
     perfis.txt. Use N_PERFIS / argv pra limitar (ex.: 100 existem, quero 25).
4. AdsPower aberto (Local API ativa em `local.adspower.net:50325`).

## Rodar
```
python swap.py
```
Ctrl+C para parar (fecha todos os perfis).

## Notas
- Fingerprint: ALEATORIO a cada abertura (modelo simples). Para fingerprint
  fixo por conta, salve o `fingerprint_config` junto da conta no pool e reaplique.
- O clicker/monitor (unmute, reload, freeze/F5) e outra camada — este script so
  cuida de QUEM esta logado em cada perfil.
- `delete-cache` exige o perfil fechado (o fluxo ja faz `stop` antes).
