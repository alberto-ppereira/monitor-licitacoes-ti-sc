"""Verificações offline: a lógica de coleta, tratamento, banco e exportação, sem acessar a internet.

As APIs são substituídas por respostas simuladas (fictícias) no formato esperado do PNCP e do IBGE.
Rodar com:  python -m tests.verificacoes
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import config as C
import main as M
from src import api, banco, exportacao, ibge, pncp, tratamento

HOJE = date.today()
d = lambda n: (HOJE + timedelta(days=n)).isoformat()


CNPJ = {"Joinville": "83169623000110", "Blumenau": "83108357000115", "Lages": "82777301000190",
        "Chapecó": "83021808000182", "Itajaí": "83102277000152", "Criciúma": "82915646000185", "Curitiba": "76417005000186", "IFSC": "11402887000160"}


def contratacao(i, objeto, municipio, ibge_cod, uf="SC", pub=-3, enc=10, valor=500000, esfera="M",
                situacao="Divulgada no PNCP"):
    return {"numeroControlePNCP": f"0000-1-{i:06d}/2026", "anoCompra": 2026, "objetoCompra": objeto,
            "modalidadeNome": "Pregão - Eletrônico", "valorTotalEstimado": valor, "valorTotalHomologado": None,
            "situacaoCompraNome": situacao, "usuarioNome": "IPM Sistemas", "dataPublicacaoPncp": d(pub) + "T09:00:00",
            "dataAberturaProposta": d(pub) + "T09:00:00", "dataEncerramentoProposta": d(enc) + "T09:00:00",
            "linkSistemaOrigem": f"https://exemplo.invalid/{i}",
            "orgaoEntidade": {"cnpj": CNPJ.get(municipio, CNPJ["IFSC"] if esfera == "F" else "0"),
                              "razaoSocial": f"MUNICIPIO DE {municipio.upper()}", "esferaId": esfera},
            "unidadeOrgao": {"ufSigla": uf, "municipioNome": municipio, "codigoIbge": ibge_cod, "nomeUnidade": "Prefeitura"}}


def contrato(i, objeto, municipio, ibge_cod, uf="SC", assin=-5, fim=200, forn="EMPRESA EXEMPLO LTDA", cat="Serviços"):
    return {"numeroControlePNCP": f"{CNPJ[municipio]}-2-{i:06d}/2026", "numeroControlePncpCompra": None, "objetoContrato": objeto,
            "niFornecedor": "11111111000111", "nomeRazaoSocialFornecedor": forn, "valorInicial": 100000,
            "valorGlobal": 120000, "dataAssinatura": d(assin), "dataVigenciaInicio": d(assin), "dataVigenciaFim": d(fim),
            "dataPublicacaoPncp": d(assin) + "T10:00:00", "tipoContrato": {"nome": "Contrato"}, "usuarioNome": "Betha Sistemas",
            "categoriaProcesso": {"nome": cat},
            "orgaoEntidade": {"cnpj": CNPJ[municipio], "razaoSocial": f"MUNICIPIO DE {municipio}", "esferaId": "M"},
            "unidadeOrgao": {"ufSigla": uf, "municipioNome": municipio, "codigoIbge": ibge_cod}}


PUB = [contratacao(1, "Licença de uso de sistema integrado de gestão pública", "Joinville", "4209102", enc=12, valor=2_000_000),
       contratacao(2, "Aquisição de toner para impressoras", "Joinville", "4209102"),
       contratacao(3, "Software de gestão da saúde e prontuário eletrônico", "Blumenau", "4202404", enc=5),
       contratacao(4, "Sistema de arrecadação tributária (IPTU e ISS)", "Lages", "", enc=-2),   # sem código, já encerrada
       contratacao(6, "Licença de uso de software de gestão acadêmica", "Florianópolis", "4205407", esfera="F"),  # federal
       contratacao(7, "Aquisição de merenda escolar", "Itajaí", "4208203"),  # não é TI, mas traz o órgão de Itajaí
       contratacao(9, "Locação de sistema web integrado de gestão pública", "Itajaí", "4208203", situacao="Suspensa")]
ABERTAS = [contratacao(5, "Contratação de sistemas informatizados de gestão escolar", "Chapecó", "4204202", pub=-40, enc=20),
           contratacao(8, "Aquisição de pneus", "Criciúma", "4204608", pub=-40, enc=20)]  # órgão visto só nas abertas
CONTRATOS = [contrato(1, "Locação de software de gestão de folha de pagamento", "Itajaí", "4208203"),
             contrato(2, "Fornecimento de merenda", "Itajaí", "4208203"),
             contrato(3, "Licença de uso de software ERP", "Curitiba", "4106902", uf="PR"),
             contrato(4, "Sistema integrado de gestão pública", "Joinville", "4209102", assin=-400, fim=100),
             contrato(5, "Prestação de serviços conforme termo de referência", "Blumenau", "4202404", cat="Informática (TIC)"),
             contrato(6, "Aquisição de toner", "Blumenau", "4202404", cat="Informática (TIC)")]
FALHAR_CNPJ = CNPJ["Chapecó"]   # simula o PNCP fora do ar para um órgão (falha sempre)
FALHAR_UMA_VEZ = {CNPJ["Blumenau"]}   # simula um 400 passageiro: falha na 1ª consulta, funciona na repetição
MUNICIPIOS = [{"id": 4209102, "nome": "Joinville"}, {"id": 4202404, "nome": "Blumenau"}, {"id": 4209300, "nome": "Lages"},
              {"id": 4204202, "nome": "Chapecó"}, {"id": 4208203, "nome": "Itajaí"}]
POP = [{"resultados": [{"series": [{"localidade": {"id": "4209102"}, "serie": {"2025": "654888"}},
                                   {"localidade": {"id": "4202404"}, "serie": {"2025": "372000"}}]}]}]
CHAMADAS = []


def api_falsa(url, params=None):
    """Simula as APIs. Pagina as contratações publicadas em 2 páginas para testar a paginação."""
    params = params or {}
    CHAMADAS.append((url, dict(params)))
    pagina = params.get("pagina", 1)
    if url.endswith("/municipios"):
        return MUNICIPIOS
    if "/agregados/" in url:
        return POP
    if url.endswith("/contratacoes/publicacao"):
        if params.get("codigoModalidadeContratacao") != 6 or params["dataFinal"] != HOJE.strftime("%Y%m%d"):
            return None  # 204: sem conteúdo
        return {"data": PUB[:2] if pagina == 1 else PUB[2:], "totalPaginas": 2}
    if url.endswith("/contratacoes/proposta"):
        return {"data": ABERTAS, "totalPaginas": 1} if params.get("codigoModalidadeContratacao") == 6 else None
    if url.endswith("/contratos"):
        if params["dataFinal"] != HOJE.strftime("%Y%m%d"):
            return None
        cnpj = params.get("cnpjOrgao")
        import requests
        if cnpj == FALHAR_CNPJ:
            raise requests.HTTPError("HTTP 503")
        if cnpj in FALHAR_UMA_VEZ:
            FALHAR_UMA_VEZ.discard(cnpj)
            raise requests.HTTPError("HTTP 400")
        dados = [c for c in CONTRATOS if not cnpj or c["orgaoEntidade"]["cnpj"] == cnpj]
        return {"data": dados, "totalPaginas": 1} if dados else None
    raise AssertionError(f"URL inesperada: {url}")


def checar(nome, condicao):
    print(("OK   " if condicao else "FALHA") + " " + nome)
    if not condicao:
        checar.falhas += 1
checar.falhas = 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    # janelas
    js = list(pncp.janelas(date(2026, 1, 1), date(2026, 1, 25), 10))
    checar("janelas cobrem o período sem buracos nem sobreposição",
           js[0][0] == date(2026, 1, 1) and js[-1][1] == date(2026, 1, 25)
           and all(b + timedelta(days=1) == c for (_, b), (c, _) in zip(js, js[1:])))
    # filtro de TI e categorias
    checar("filtro de TI reconhece software de gestão", tratamento.eh_ti("Licença de uso de software de gestão"))
    checar("filtro de TI exclui toner", not tratamento.eh_ti("Aquisição de toner para impressoras"))
    checar("filtro de TI não casa 'erp' dentro de outra palavra", not tratamento.eh_ti("Serviços de serpentina"))
    checar("filtro de TI: 'sistema web integrado' (objeto típico da IPM) é TI",
           tratamento.eh_ti("Locação de sistema web integrado de gestão pública municipal, em nuvem"))
    checar("filtro de TI: 'cadastro mobiliário' não derruba um ERP; aquisição de mobiliário continua fora",
           tratamento.eh_ti("Locação de sistema ERP: cadastro imobiliário, cadastro mobiliário e ISS")
           and not tratamento.eh_ti("Aquisição de mobiliário e software de escritório"))
    import pandas as _pd
    _o = _pd.DataFrame({"esfera": ["M", "N", "N", "F"], "orgao": ["MUNICIPIO X", "CONSORCIO INTERFEDERATIVO SANTA CATARINA - CINCATARINA",
                                                               "CAIXA ECONOMICA FEDERAL", "IFSC"]})
    checar("esfera: municípios e consórcios entram; Caixa (N) e federal ficam fora",
           tratamento.na_esfera(_o).tolist() == [True, True, False, False])
    checar("tipo de órgão pelo nome (prefeitura, câmara, fundo, fundação, autarquia, consórcio)",
           [tratamento.tipo_orgao(n) for n in ["MUNICIPIO DE BRUSQUE", "CAMARA MUNICIPAL DE PORTO BELO",
            "FUNDO MUNICIPAL DE SAUDE DE PENHA", "FUNDACAO CULTURAL DE BRUSQUE", "INSTITUTO BRUSQUENSE DE PREVIDENCIA - IBPREV",
            "SERVICO AUTONOMO MUNICIPAL DE AGUA E ESGOTO", "CONSORCIO INTERFEDERATIVO SANTA CATARINA - CINCATARINA"]]
           == ["Prefeitura", "Câmara", "Fundo", "Fundação", "Autarquia", "Autarquia", "Consórcio"])
    checar("situação: suspensa e revogada deixam de ser oportunidade",
           tratamento.situacao_encerrada("Suspensa") and tratamento.situacao_encerrada("Revogada")
           and not tratamento.situacao_encerrada("Divulgada no PNCP"))
    checar("categoria por palavra-chave", tratamento.categoria("Sistema de arrecadação de IPTU") == "Tributos"
           and tratamento.categoria("prontuário eletrônico") == "Saúde")

    # acesso HTTP: 400 do PNCP é repetido algumas vezes; 404 não é repetido
    import types
    import requests

    class Resp:
        def __init__(self, status, corpo=None):
            self.status_code, self._corpo, self.content = status, corpo, b"{}"
        def json(self):
            return self._corpo
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    class Sessao:
        def __init__(self, respostas):
            self.respostas, self.chamadas = list(respostas), 0
        def get(self, *a, **k):
            self.chamadas += 1
            return self.respostas.pop(0)

    C.PAUSA_ENTRE_CHAMADAS_S = 0
    tempo_real, sessao_real = api.time, api._sessao
    api.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=tempo_real.monotonic)
    falsa = Sessao([Resp(400), Resp(200, {"data": [1]})])
    api._sessao = lambda: falsa
    checar("400 passageiro do PNCP é repetido e a consulta funciona", api.get_json("u") == {"data": [1]} and falsa.chamadas == 2)
    falsa = Sessao([Resp(404), Resp(200, {})])
    try:
        api.get_json("u")
        r404 = False
    except requests.HTTPError:
        r404 = falsa.chamadas == 1
    checar("404 não é repetido", r404)
    api.time, api._sessao = tempo_real, sessao_real

    # coleta de ponta a ponta com APIs simuladas
    api.get_json = ibge.get_json = api_falsa
    C.ESPERA_ANTES_DE_REPETIR_S = 0
    with tempfile.TemporaryDirectory() as tmp:
        C.BANCO, C.SAIDA, C.DASHBOARD = Path(tmp) / "monitor.db", Path(tmp) / "saida", Path(tmp) / "sem_dashboard"
        C.PAUSA_ENTRE_CHAMADAS_S = 0
        con = banco.conectar(C.BANCO)
        M.coletar(con, dias=15)
        k1 = banco.ler(con, "contratacoes")
        k2 = banco.ler(con, "contratos")
        checar("paginação leu as 2 páginas", any(p.get("pagina") == 2 for _, p in CHAMADAS))
        checar("banco guarda as municipais com a marcação de TI (TI = 1, 3, 4, 5; federal fora)",
               set(k1.loc[k1["ti"] == 1, "id_pncp"].str[-11:-5]) == {"000001", "000003", "000004", "000005", "000009"}
               and "000002" in set(k1["id_pncp"].str[-11:-5]) and "000006" not in set(k1["id_pncp"].str[-11:-5]))
        orgaos = banco.ler(con, "orgaos")
        checar("órgãos de todas as contratações guardados para buscar contratos (inclui Itajaí, não TI)",
               CNPJ["Itajaí"] in set(orgaos["orgao_cnpj"]))
        checar("código IBGE completado pelo nome (Lages)", k1.loc[k1["municipio"] == "Lages", "codigo_ibge"].iloc[0] == "4209300")
        checar("contratos por órgão: TI por termo e por categoria TIC, toner fora",
               set(k2.loc[k2["ti"] == 1, "id_pncp"].str[-11:-5]) == {"000001", "000004", "000005"})
        checar("órgão com 400 passageiro é recuperado na repetição do fim da etapa (Blumenau)",
               "000005" in set(k2["id_pncp"].str[-11:-5]))
        checar("órgão que falha sempre fica pendente, sem derrubar a coleta (Chapecó)",
               len(pncp.orgaos_pendentes) == 1 and "CHAPEC" in pncp.orgaos_pendentes[0] and not pncp.falhas)
        org = banco.ler(con, "orgaos").set_index("orgao_cnpj")["contratos_ate"]
        checar("data de consulta dos contratos avança por órgão (Chapecó não avança)",
               org[CNPJ["Joinville"]] == HOJE.isoformat() and org.isna()[CNPJ["Chapecó"]])
        ex = pd.read_sql_query("SELECT * FROM execucoes", con)
        checar("falha só em contratos não trava a data da última coleta", ex.iloc[-1]["fim"] == HOJE.isoformat())
        M.coletar(con, dias=15)
        checar("rodar de novo não duplica", len(banco.ler(con, "contratacoes")) == len(k1) and len(banco.ler(con, "contratos")) == len(k2))
        org2 = banco.ler(con, "orgaos").set_index("orgao_cnpj")["contratos_ate"]
        checar("regravar os órgãos não apaga a data de consulta dos contratos", org2[CNPJ["Joinville"]] == HOJE.isoformat())
        # execução incremental: propostas em aberto já consultadas hoje e Criciúma sem novidade ficam de fora
        CHAMADAS.clear()
        M.coletar(con, None)
        urls = [(u, p) for u, p in CHAMADAS]
        checar("incremental: propostas em aberto puladas (já consultadas hoje)",
               not any(u.endswith("/contratacoes/proposta") for u, _ in urls))
        cnpjs = {p.get("cnpjOrgao") for u, p in urls if u.endswith("/contratos")}
        checar("incremental: órgão sem novidade não é reconsultado; pendente (Chapecó) é",
               CNPJ["Criciúma"] not in cnpjs and CNPJ["Chapecó"] in cnpjs and CNPJ["Joinville"] in cnpjs)
        con.execute("ALTER TABLE contratos DROP COLUMN categoria_processo")
        con.close()
        con = banco.conectar(C.BANCO)
        checar("banco antigo recebe as colunas novas", "categoria_processo" in {r[1] for r in con.execute("PRAGMA table_info(contratos)")})
        import sqlite3
        antigo = sqlite3.connect(Path(tmp) / "antigo.db")
        antigo.execute("CREATE TABLE contratacoes (id_pncp TEXT PRIMARY KEY, objeto TEXT, codigo_ibge TEXT)")
        antigo.execute("INSERT INTO contratacoes VALUES ('x', 'software', NULL)")
        antigo.commit(); antigo.close()
        antigo = banco.conectar(Path(tmp) / "antigo.db")
        checar("registros antigos (só de TI) recebem ti = 1", antigo.execute("SELECT ti FROM contratacoes").fetchone()[0] == 1)
        antigo.close()
        # reclassificar com um único termo de TI: Joinville (sistema integrado) fica, Blumenau (saúde) sai
        termos = list(C.TERMOS_TI)
        tratamento._TI = tratamento._regex(["sistema integrado"])
        M.reclassificar(con)
        k3 = banco.ler(con, "contratacoes").set_index("id_pncp")["ti"]
        checar("reclassificar refaz a marcação sem baixar nada",
               k3["0000-1-000003/2026"] == 0 and k3["0000-1-000001/2026"] == 1)
        tratamento._TI = tratamento._regex(termos)
        M.reclassificar(con)
        # carga de órgãos a partir de uma exportação do portal do PNCP (só SC entra)
        csv = Path(tmp) / "portal.csv"
        cab = list(M.CSV_PORTAL)
        linhas = [["11222333000144", "CAMARA MUNICIPAL DE LAGES", "M", "4209300", "Lages", "SC"],
                  ["76417005000186", "MUNICIPIO DE CURITIBA", "M", "4106902", "Curitiba", "PR"]]
        csv.write_text("\ufeff" + ";".join(cab + ["Outra coluna"]) + "\n" +
                       "\n".join(";".join(l + ["x"]) for l in linhas) + "\n", encoding="utf-8")
        M.carregar_orgaos(con, None, [str(csv)])
        org3 = set(banco.ler(con, "orgaos")["orgao_cnpj"])
        checar("carga de órgãos pelo CSV do portal: câmara de SC entra, órgão do PR não",
               "11222333000144" in org3 and "76417005000186" not in org3 and banco.ler_controle(con, "carga_orgaos"))
        M.carregar_orgaos(con, 30, None)
        checar("carga de órgãos por período percorre as licitações publicadas", CNPJ["Joinville"] in set(banco.ler(con, "orgaos")["orgao_cnpj"]))
        M.exportar(con)
        con.close()
        saida = C.SAIDA
        op = pd.read_csv(saida / "oportunidades.csv", dtype={"codigo_ibge": str})
        mv = pd.read_csv(saida / "movimentacoes.csv")
        ct = pd.read_csv(saida / "contratos.csv")
        checar("arquivos gerados", all((saida / f).exists() for f in
               ["oportunidades.csv", "movimentacoes.csv", "contratos.csv", "monitor_licitacoes.xlsx", "dados.js"]))
        checar("colunas no formato do dashboard", list(op.columns) == exportacao.COLS_OPORTUNIDADES
               and list(mv.columns) == exportacao.COLS_MOVIMENTACOES and list(ct.columns) == exportacao.COLS_CONTRATOS)
        checar("oportunidades só com disputa a partir de hoje (Lages, encerrada, fora)", "Lages" not in set(op["municipio"]) and len(op) == 3)
        checar("licitação suspensa não aparece como oportunidade (Itajaí)", "Itajaí" not in set(op["municipio"]))
        checar("plataforma de publicação guardada e exportada", set(op["plataforma"]) == {"IPM Sistemas"}
               and set(ct["plataforma"]) == {"Betha Sistemas"})
        checar("contratos exportados com link do PNCP e tipo de órgão",
               ct["link"].str.startswith("https://pncp.gov.br/contratos/").all() and set(ct["tipo_orgao"]) == {"Prefeitura"})
        checar("notas entre 0 e 10, maior para Joinville (porte e valor)", op["nota"].between(0, 10).all() and op.iloc[0]["municipio"] == "Joinville")
        checar("movimentações só dos últimos 15 dias (contrato de 400 dias fora)",
               set(mv["tipo"]) == {"Licitação publicada", "Contrato assinado"} and (mv["data"] >= d(-14)).all())
    print(f"\n{checar.falhas} falha(s).")
    sys.exit(1 if checar.falhas else 0)


if __name__ == "__main__":
    main()
