"""Verificações offline do fornecedor_no_mapa.py, com a API do PNCP e do IBGE simuladas.

Rodar com:  python -m tests.verificacoes_fornecedor
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import requests

import config as C
import fornecedor_no_mapa as F
from src import api, banco, ibge

HOJE = date.today()
d = lambda n: (HOJE + timedelta(days=n)).isoformat()
FORN, FORN_FILIAL, OUTRA = "12345678000141", "12345678000303", "99888777000166"
ORGAOS = [("83169623000110", "MUNICIPIO DE JOINVILLE", "Joinville", "4209102"),
          ("83108357000115", "MUNICIPIO DE BLUMENAU", "Blumenau", "4202404"),
          ("11222333000144", "CAMARA MUNICIPAL DE LAGES", "Lages", "4209300"),
          ("83021808000182", "MUNICIPIO DE CHAPECO", "Chapecó", "4204202")]
CONTRATOS = {  # CNPJ do órgão -> (contrato, dias desde a publicação)
    "83169623000110": [("Locação de sistema de gestão pública integrada", FORN, "SOFTWARE PUBLICO A LTDA", -300, 200),
                       ("Aquisição de pneus", OUTRA, "PNEUS SA", -20, 100)],
    "83108357000115": [("Licença de uso de software de gestão tributária", OUTRA, "OUTRA SOFTWARE LTDA", -100, 300)],
    "11222333000144": [("Sistema de gestão legislativa", FORN_FILIAL, "SOFTWARE PUBLICO A LTDA", -700, -30)],  # filial, encerrado
}
FALHAR = {"83021808000182"}   # Chapecó: PNCP fora do ar na primeira rodada
CHAMADAS = []


def contrato(orgao, i, objeto, forn_cnpj, forn, pub, fim):
    cnpj, nome, mun, cod = next(o for o in ORGAOS if o[0] == orgao)
    return {"numeroControlePNCP": f"{orgao}-2-{i:06d}/2026", "numeroControlePncpCompra": None, "objetoContrato": objeto,
            "niFornecedor": forn_cnpj, "nomeRazaoSocialFornecedor": forn, "valorInicial": 1000, "valorGlobal": 120000,
            "dataAssinatura": d(pub), "dataVigenciaInicio": d(pub), "dataVigenciaFim": d(fim),
            "dataPublicacaoPncp": d(pub) + "T10:00:00", "tipoContrato": {"nome": "Contrato"},
            "categoriaProcesso": {"nome": "Serviços"}, "orgaoEntidade": {"cnpj": cnpj, "razaoSocial": nome, "esferaId": "M"},
            "unidadeOrgao": {"ufSigla": "SC", "municipioNome": mun, "codigoIbge": cod}}


def api_falsa(url, params=None):
    params = params or {}
    if url.endswith("/municipios"):
        return [{"id": int(o[3]), "nome": o[2]} for o in ORGAOS]
    assert url.endswith("/v1/contratos"), url
    CHAMADAS.append(dict(params))
    a = date(int(params["dataInicial"][:4]), int(params["dataInicial"][4:6]), int(params["dataInicial"][6:]))
    b = date(int(params["dataFinal"][:4]), int(params["dataFinal"][4:6]), int(params["dataFinal"][6:]))
    if (b - a).days > 200:   # simula a API recusando intervalos longos
        raise requests.HTTPError("400 Client Error", response=type("R", (), {"status_code": 400})())
    if params["cnpjOrgao"] in FALHAR:
        raise requests.HTTPError("HTTP 503", response=type("R", (), {"status_code": 503})())
    dados = [contrato(params["cnpjOrgao"], i, *c) for i, c in enumerate(CONTRATOS.get(params["cnpjOrgao"], []), 1)]
    dados = [x for x in dados if a.isoformat() <= x["dataPublicacaoPncp"][:10] <= b.isoformat()]
    return {"data": dados, "totalPaginas": 1} if dados else None


def checar(nome, ok):
    print(("OK   " if ok else "FALHA") + " " + nome)
    checar.falhas += not ok
checar.falhas = 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    checar("lacunas: só o que falta é consultado",
           F.lacunas(date(2026, 1, 1), date(2026, 3, 31), [(date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 3, 1), date(2026, 3, 31))])
           == [(date(2026, 2, 1), date(2026, 2, 28))])
    checar("link do contrato no portal do PNCP",
           F.link_pncp("10479670000196-2-000915/2026") == "https://pncp.gov.br/contratos/10479670000196/2026/915")

    api.get_json = ibge.get_json = api_falsa
    C.PAUSA_ENTRE_CHAMADAS_S, C.LOG_POR_PAGINA = 0, False
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        C.BANCO, C.BANCO_HISTORICO, C.SAIDA, C.DASHBOARD = t / "monitor.db", t / "hist.db", t / "saida", t / "dash"
        m = banco.conectar(C.BANCO)
        banco.gravar(m, "orgaos", __import__("pandas").DataFrame(
            [{"orgao_cnpj": c, "orgao": n, "esfera": "M", "uf": "SC", "municipio": mu, "codigo_ibge": co} for c, n, mu, co in ORGAOS]))
        m.close()
        F.janela_atual = 365
        con = F.abrir_cache()
        desde = HOJE - timedelta(days=800)
        F.coletar(con, desde, ["12345678"], False, None)
        checar("API recusou 365 dias: o período foi dividido e a janela diminuiu", F.janela_atual <= 200)
        ct = banco.ler(con, "contratos")
        checar("contratos do fornecedor encontrados, inclusive de filial (raiz do CNPJ)",
               set(ct.loc[ct["fornecedor_cnpj"].str.startswith("12345678"), "orgao_cnpj"]) == {"83169623000110", "11222333000144"})
        checar("cache guarda os outros contratos de TI e descarta os que não são de TI (pneus)",
               "OUTRA SOFTWARE LTDA" in set(ct["fornecedor"]) and "PNEUS SA" not in set(ct["fornecedor"]))
        feitos = {r[0] for r in con.execute("SELECT DISTINCT orgao_cnpj FROM consultas")}
        checar("órgão que falhou não fica marcado como consultado (Chapecó)", "83021808000182" not in feitos and len(feitos) == 3)
        CHAMADAS.clear()
        FALHAR.clear()
        F.coletar(con, desde, ["12345678"], False, None)
        checar("segunda execução consulta só o que faltava (Chapecó)", {c["cnpjOrgao"] for c in CHAMADAS} == {"83021808000182"})
        F.exportar(con, ["12345678"], "Sistema A", desde)
        js = (C.DASHBOARD / "dados_fornecedor.js").read_text(encoding="utf-8")
        dados = json.loads(js[js.index("{"):js.rindex("}") + 1])
        vig = {c["municipio"]: c["vigente"] for c in dados["contratos"]}
        checar("exportação: Joinville vigente, Lages encerrado", vig == {"Joinville": True, "Lages": False})
        checar("exportação: Blumenau aparece como outro fornecedor de TI", [o["municipio"] for o in dados["outros"]] == ["Blumenau"])
        checar("CSVs gerados", all((C.SAIDA / f).exists() for f in
               ["fornecedor_sistema_a_contratos.csv", "fornecedor_sistema_a_municipios.csv"]))
        con.close()
    print(f"\n{checar.falhas} falha(s).")
    sys.exit(1 if checar.falhas else 0)


if __name__ == "__main__":
    main()
