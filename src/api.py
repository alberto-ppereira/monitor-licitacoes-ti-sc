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


def _sessao() -> requests.Session:
    s = getattr(_local, "sessao", None)
    if s is None:
        s = _local.sessao = requests.Session()
        s.headers.update({"Accept": "application/json", "User-Agent": "monitor-licitacoes (projeto pessoal de estudo)"})
    return s
ESTATISTICAS = {"chamadas": 0, "novas_tentativas": 0, "segundos_em_rede": 0.0}


def get_json(url: str, params: dict | None = None):
    """GET com pausa mínima entre chamadas e novas tentativas com espera crescente.

    Devolve o JSON, ou None quando a API responde 204 (sem conteúdo). Pode ser chamada de várias
    linhas de execução ao mesmo tempo: a pausa mínima vale para o conjunto das chamadas.
    """
    global _ultima
    for tentativa in range(1, C.TENTATIVAS + 1):
        with _trava:
            espera = C.PAUSA_ENTRE_CHAMADAS_S - (time.monotonic() - _ultima)
            if espera > 0:
                time.sleep(espera)
            _ultima = time.monotonic()
        try:
            t0 = time.monotonic()
            r = _sessao().get(url, params=params, timeout=C.TIMEOUT_S)
            dt = time.monotonic() - t0
            with _trava:
                ESTATISTICAS["chamadas"] += 1
                ESTATISTICAS["segundos_em_rede"] += dt
            log.debug("GET %s %s -> HTTP %s em %.1fs (%d bytes)", url.rsplit("/api/", 1)[-1], params or "",
                      r.status_code, dt, len(r.content))
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
            if tentativa == C.TENTATIVAS:
                raise
            pausa = min(2 ** tentativa, C.ESPERA_MAXIMA_S)
            with _trava:
                ESTATISTICAS["novas_tentativas"] += 1
            log.warning("Falha em %s (%s). Tentativa %d de %d; nova tentativa em %ss.",
                        url.rsplit("/api/", 1)[-1], e, tentativa,
                        C.TENTATIVAS_ERRO_400 if status == 400 else C.TENTATIVAS, pausa)
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
