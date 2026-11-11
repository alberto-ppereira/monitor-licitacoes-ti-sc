"""Municípios e população pelas APIs do IBGE."""
import logging

import pandas as pd

import config as C
from src.api import get_json

log = logging.getLogger(__name__)


def municipios(codigo_uf: int = C.CODIGO_UF_IBGE) -> pd.DataFrame:
    """Lista oficial de municípios da UF: codigo_ibge (7 dígitos) e nome."""
    dados = get_json(f"{C.IBGE_API}/v1/localidades/estados/{codigo_uf}/municipios") or []
    return pd.DataFrame({"codigo_ibge": [str(m["id"]) for m in dados], "municipio": [m["nome"] for m in dados]})


def populacao(codigo_uf: int = C.CODIGO_UF_IBGE) -> pd.DataFrame:
    """População estimada mais recente por município (agregado 6579, variável 9324 do SIDRA).

    Se a API mudar ou falhar, devolve tabela vazia: a população só refina a pontuação.
    """
    url = f"{C.IBGE_API}/v3/agregados/6579/periodos/-1/variaveis/9324"
    try:
        corpo = get_json(url, {"localidades": f"N6[N3[{codigo_uf}]]"})
        series = corpo[0]["resultados"][0]["series"]
        linhas = [{"codigo_ibge": s["localidade"]["id"], "populacao": int(next(iter(s["serie"].values())))}
                  for s in series]
        return pd.DataFrame(linhas)
    except Exception as e:  # noqa: BLE001 - dado opcional
        log.warning("População do IBGE indisponível (%s); seguindo sem ela.", e)
        return pd.DataFrame(columns=["codigo_ibge", "populacao"])
