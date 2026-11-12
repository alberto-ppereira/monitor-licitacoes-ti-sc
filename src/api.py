"""Acesso HTTP às APIs públicas: pausa entre chamadas, novas tentativas e paginação."""
import logging
import threading
import time
from typing import Iterator

import requests

import config as C

log = logging.getLogger(__name__)
_local = threading.local()     # uma sessão (conexão reaproveitada) por linha de execução
_trava = threading.Lock()      # protege a pausa entre chamadas e as estatísticas quando há consultas paralelas
_ultima = 0.0
_freio = {"extra": 0.0, "ate": 0.0}   # intervalo extra entre chamadas e pausa geral, ajustados pelo HTTP 429


def _sessao() -> requests.Session:
    s = getattr(_local, "sessao", None)
    if s is None:
        s = _local.sessao = requests.Session()
        s.headers.update({"Accept": "application/json, text/plain, */*", "Referer": "https://pncp.gov.br/app/contratos",
                          "User-Agent": "monitor-licitacoes (projeto pessoal de estudo)"})
    return s
ESTATISTICAS = {"chamadas": 0, "novas_tentativas": 0, "segundos_em_rede": 0.0, "desaceleracoes": 0}


def get_json(url: str, params: dict | None = None):
    """GET com pausa mínima entre chamadas e novas tentativas com espera crescente.

    Devolve o JSON, ou None quando a API responde 204 (sem conteúdo). Pode ser chamada de várias
    linhas de execução ao mesmo tempo: a pausa mínima vale para o conjunto das chamadas.

    Freio adaptativo: quando o servidor responde 429 (muitas requisições), todas as consultas param pelo
    tempo pedido (Retry-After) e o intervalo entre chamadas dobra, até PAUSA_MAXIMA_S. A cada resposta
    normal, o intervalo extra volta a cair aos poucos.
    """
    global _ultima
    tentativa = 0
    while True:
        tentativa += 1
        with _trava:
            agora = time.monotonic()
            espera = max(C.PAUSA_ENTRE_CHAMADAS_S + _freio["extra"] - (agora - _ultima), _freio["ate"] - agora)
            if espera > 0:
                time.sleep(espera)
            _ultima = time.monotonic()
        try:
            t0 = time.monotonic()
            extra = {"Connection": "close"} if "/api/search" in url else None  # a busca derruba conexões reaproveitadas
            r = _sessao().get(url, params=params, timeout=C.TIMEOUT_S, headers=extra)
            dt = time.monotonic() - t0
            with _trava:
                ESTATISTICAS["chamadas"] += 1
                ESTATISTICAS["segundos_em_rede"] += dt
            log.debug("GET %s %s -> HTTP %s em %.1fs (%d bytes)", url.rsplit("/api/", 1)[-1], params or "",
                      r.status_code, dt, len(r.content))
            with _trava:
                if r.status_code != 429:
                    _freio["extra"] = max(0.0, _freio["extra"] * 0.95 - 0.01)
            if r.status_code == 204:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                # erro do pedido: repetir não adianta. Exceção: o PNCP às vezes devolve 400 por instabilidade
                if status != 400 or tentativa >= C.TENTATIVAS_ERRO_400:
                    raise
            conexao = isinstance(e, requests.ConnectionError)
            limite = (C.TENTATIVAS_429 if status == 429 else C.TENTATIVAS_ERRO_400 if status == 400
                      else C.TENTATIVAS_CONEXAO if conexao else C.TENTATIVAS)
            if tentativa >= limite:
                raise
            pausa = min(2 ** tentativa, C.ESPERA_MAXIMA_S)
            if isinstance(e, requests.ConnectionError):
                # conexão derrubada pelo servidor: fecha as conexões abertas (mantendo os cookies da sessão,
                # que o servidor usa como proteção) e tenta logo, numa conexão nova
                _sessao().close()
                pausa = 1 if tentativa <= 3 else 3
            with _trava:
                ESTATISTICAS["novas_tentativas"] += 1
                if status == 429:
                    resposta = getattr(e, "response", None)
                    pedido = str(getattr(resposta, "headers", {}).get("Retry-After") or "") if resposta is not None else ""
                    pausa = float(pedido) if pedido.replace(".", "", 1).isdigit() else min(5 * tentativa, C.ESPERA_MAXIMA_S)
                    _freio["ate"] = max(_freio["ate"], time.monotonic() + pausa)
                    _freio["extra"] = min(max(_freio["extra"] * 2, 0.5), C.PAUSA_MAXIMA_S)
                    ESTATISTICAS["desaceleracoes"] += 1
            if status == 429:
                log.warning("PNCP pediu para desacelerar (HTTP 429). Todas as consultas esperam %.0fs; intervalo entre "
                            "chamadas agora %.1fs. Tentativa %d de %d.", pausa,
                            C.PAUSA_ENTRE_CHAMADAS_S + _freio["extra"], tentativa, limite)
                continue  # a espera geral acontece no início do laço
            log.warning("Falha em %s (%s). Tentativa %d de %d; nova tentativa em %ss.",
                        url.rsplit("/api/", 1)[-1], e, tentativa, limite, pausa)
            time.sleep(pausa)


def paginar(url: str, params: dict, rotulo: str = "") -> Iterator[dict]:
    """Percorre todas as páginas de um endpoint do PNCP que devolve {data, totalPaginas, ...}.

    Com LOG_POR_PAGINA, registra cada página lida (número, total, registros e tempo), para que a coleta
    nunca fique minutos em silêncio.
    """
    pagina = 1
    while True:
        t0 = time.monotonic()
        corpo = get_json(url, {**params, "pagina": pagina})
        segundos = time.monotonic() - t0
        if not corpo or not corpo.get("data"):
            if pagina == 1 and C.LOG_POR_PAGINA:
                log.info("   %s: nenhum registro (%.1fs)", rotulo or url, segundos)
            return
        total = corpo.get("totalPaginas") or 1
        if C.LOG_POR_PAGINA:
            log.info("   %s: página %d de %d, %d registros (%.1fs)", rotulo or url, pagina, total,
                     len(corpo["data"]), segundos)
        yield from corpo["data"]
        if pagina >= total:
            return
        pagina += 1
