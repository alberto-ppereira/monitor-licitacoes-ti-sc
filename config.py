"""Configuração do monitor. Nenhum número ou texto de regra de negócio fica fora deste arquivo."""
from pathlib import Path

RAIZ = Path(__file__).parent
BANCO = RAIZ / "data" / "monitor.db"
SAIDA = RAIZ / "saida"
DASHBOARD = RAIZ / "dashboard"   # o dados.js também é copiado para cá, ao lado do dashboard.html

# Escopo
UF = "SC"
CODIGO_UF_IBGE = 42

# APIs públicas
PNCP_CONSULTA = "https://pncp.gov.br/api/consulta"
# Busca textual do portal do PNCP (a mesma da tela de pesquisa). Não faz parte da API documentada:
# endereço e campos podem mudar. Usada só pelo comando `buscar` (carga histórica por palavra-chave).
PNCP_BUSCA = "https://pncp.gov.br/api/search/"
PNCP_INTEGRACAO = "https://pncp.gov.br/api/pncp"   # API de integração: termos aditivos de cada contrato
BUSCA_TAMANHO_PAGINA = 100
BUSCA_MAX_RESULTADOS = 10000   # limite típico de paginação da busca; janelas maiores são divididas
BUSCA_JANELA_DIAS = 92         # período de assinatura por consulta (um trimestre)
IBGE_API = "https://servicodados.ibge.gov.br/api"

# Coleta no PNCP
# Códigos de modalidade da API de consulta do PNCP (conferir na documentação):
# 4 Concorrência eletrônica, 5 Concorrência presencial, 6 Pregão eletrônico, 7 Pregão presencial,
# 8 Dispensa, 9 Inexigibilidade, 12 Credenciamento.
MODALIDADES = [4, 5, 6, 7, 8, 9]
NOMES_MODALIDADES = {4: "concorrência eletrônica", 5: "concorrência presencial", 6: "pregão eletrônico",
                     7: "pregão presencial", 8: "dispensa", 9: "inexigibilidade", 12: "credenciamento"}
JANELA_DIAS = 10              # a API limita o intervalo de datas por consulta; a coleta divide o período
TAMANHO_PAGINA_CONTRATACOES = 50
TAMANHO_PAGINA_CONTRATOS = 500
PAUSA_ENTRE_CHAMADAS_S = 0.4   # respeitar o servidor público
TENTATIVAS = 6                 # novas tentativas em erro de rede ou 429/5xx
ESPERA_MAXIMA_S = 60           # espera entre tentativas: 2, 4, 8, 16, 32, 60 s
TIMEOUT_S = 60
TENTATIVAS_ERRO_400 = 3        # o PNCP às vezes devolve 400 por instabilidade, com o pedido correto: repetir
CONSULTAS_PARALELAS = 4        # órgãos consultados ao mesmo tempo na etapa de contratos (1 = um por vez)
CONSULTAS_PARALELAS_LICITACOES = 2   # o endpoint de licitações limita o ritmo (HTTP 429) com 4 ao mesmo tempo
TENTATIVAS_CONEXAO = 12        # conexão derrubada pelo servidor (comum na busca do portal): tentativas curtas e várias
TENTATIVAS_429 = 10            # "muitas requisições": o monitor desacelera e espera mais vezes antes de desistir
PAUSA_MAXIMA_S = 5.0           # teto do intervalo entre chamadas quando o servidor pede para desacelerar
ESPERA_ANTES_DE_REPETIR_S = 30 # pausa antes de repassar, no fim da etapa, os órgãos que falharam

# Coleta incremental (sem --dias)
# Órgão sem contratação nova no período só tem os contratos reconsultados depois de N dias (0 = todos sempre).
# Nenhum contrato se perde: cada órgão guarda até que data já foi consultado e retoma dali.
REVISAR_TODOS_OS_ORGAOS_DIAS = 7
# As licitações novas já chegam pelas publicadas; as antigas ainda abertas já estão no banco.
PROPOSTAS_EM_ABERTO_UMA_VEZ_POR_DIA = True

# Log
LOG_POR_PAGINA = True          # registra cada página lida; False deixa o log mais curto
ARQUIVO_LOG = RAIZ / "data" / "coleta.log"   # log acumulado das execuções (erros incluídos)
LOG_TAMANHO_MAXIMO = 5_000_000   # bytes; ao passar disso, vira coleta.log.1 e começa um novo
LOG_ARQUIVOS_ANTIGOS = 5         # quantos arquivos antigos (coleta.log.1 a .5) são mantidos

# Lista de órgãos. Os contratos são buscados por órgão, e a lista nasce das licitações publicadas. Mas
# câmaras, fundos e fundações costumam assinar contratos de licitações "multientidade" feitas pela
# prefeitura, sem publicar licitação própria: sem uma carga ampla, ficam fora (conferido: só 55% dos
# órgãos com contrato de um grande fornecedor de ERP em SC estavam na lista). `python main.py orgaos` faz essa carga.
CARGA_ORGAOS_DIAS = 365

# Situações em que a licitação deixa de ser oportunidade (comparação sem acento, parte do nome basta)
SITUACOES_FORA = ["revogada", "anulada", "suspensa", "deserta", "fracassada"]

# Esferas de governo mantidas (campo orgaoEntidade.esferaId do PNCP): M municipal, E estadual, F federal
ESFERAS = {"M"}
# Consórcios intermunicipais vêm com esfera "N" (não se aplica) no PNCP, mas compram para os municípios
# (ex.: CINCATARINA). Com True, entram os órgãos de esfera N cujo nome contenha "consórcio".
INCLUIR_CONSORCIOS = True   # inclui também agências e serviços intermunicipais (esfera N, "intermunicipal" no nome)

# Contratos: buscar por órgão (CNPJ dos órgãos municipais de SC já vistos nas contratações) em vez de
# percorrer os contratos do país inteiro. Muito mais rápido e menos sujeito a falhas do servidor.
CONTRATOS_POR_ORGAO = True

# Filtro de TI: o objeto precisa conter pelo menos um termo (comparação sem acento e sem maiúsculas)
TERMOS_TI = [
    "sistema de gestao", "sistemas de gestao", "software", "licenca de uso", "licenciamento de uso",
    "sistema integrado", "sistemas integrados", "solucao tecnologica", "plataforma digital",
    "sistema informatizado", "sistemas informatizados", "computacao em nuvem", "erp",
    "sistema de arrecadacao", "sistema de tributacao", "sistema de folha", "sistema de contabilidade",
    "sistema de protocolo", "prontuario eletronico", "nota fiscal eletronica", "nota fiscal de servicos eletronica",
    # acrescentados após conferir 207 contratos de um fornecedor de ERP exportados do portal do PNCP (58 escapavam)
    "sistema web", "sistemas web", "integrado de gestao", "integrada de gestao", "integrado para gestao",
    "solucao informatizada", "solucoes informatizadas", "plataforma web", "tecnologia da informacao",
    "tecnologia de informacao", "siafic", "sistemas estruturantes", "atende.net", "data center", "datacenter",
    "cessao de direito de uso", "direito de uso de sistema", "locacao de sistema", "sistema de informacoes",
    "sistema unico e integrado", "portal da transparencia", "portal institucional",
    # acrescentados após conferir 88 contratos de um segundo fornecedor de ERP em SC
    "sistema de informatica", "sistemas de informatica", "sistema gestao publica",
    # acrescentados após conferir 3.714 contratos de 2026 exportados do portal
    "programas de informatica", "solucao web", "website", "site institucional", "hospedagem em nuvem", "hospedagem de site",
    "hospedagem do sistema", "hospedagem de sistema", "hospedagem, armazenamento", "e-sus",
    "licitacao em meio digital", "plataforma de licitacoes", "desenvolvimento de sistema", "arquitetura de sistema", "cessao de uso do sistema",
    "cessao de uso de sistema", "gestao educacional", "solucao de gestao",
]
# Termos fortes: indicam software de gestão com segurança e valem mesmo com termo de exclusão no texto
# (ex.: ERP com módulo de "bens móveis e imóveis", gestão documental com "digitalização", sistema com
# "controle de acesso" de usuários).
TERMOS_TI_FORTES = [
    "integrado de gestao", "integrada de gestao", "erp", "siafic", "sistemas estruturantes",
    "licenca de uso", "cessao de direito de uso", "sistema de informatica", "sistemas de informatica", "sistema web",
    "software de gestao", "programas de informatica",
]
# Categoria do processo no PNCP que já indica TI (aceita mesmo sem termo de TI no objeto)
CATEGORIAS_PROCESSO_TI = ["informatica"]
# Termos que derrubam o registro mesmo com termo de TI (falsos positivos comuns)
# Frases específicas: "mobiliario" sozinho derrubava "cadastro mobiliário" (cadastro de ISS, comum em ERP)
TERMOS_EXCLUIR = ["impressora", "toner", "material de expediente", "aquisicao de mobiliario", "aquisicao de moveis",
                  "aquisicao de veiculo", "locacao de veiculo",
                  # falsos positivos vistos na primeira coleta real: equipamentos, obras e serviços com "software" incluso
                  "rastreamento", "telemetria", "videomonitoramento", "seguranca eletronica", "controle de acesso",
                  "datalogger", "sonorizacao", "geracao distribuida", "energia eletrica", "energias renovaveis",
                  # falsos positivos vistos no painel (nov/2026): o objeto cita "sistema", mas não é software de gestão
                  "iso 9001", "gestao da qualidade", "instituicao financeira", "analises clinicas", "sigtap",
                  "alarme", "cftv", "radiocomunicacao", "educacao ambiental", "materiais didaticos",
                  "processo seletivo", "concurso publico", "servicos de reforma", "componentes de hardware",
                  "digitalizacao", "medicina e seguranca do trabalho", "seguranca do trabalho"]

# Categorias por palavras-chave, na ordem de prioridade (a primeira que casar vence)
CATEGORIAS = {
    "Saúde": ["saude", "prontuario", "ubs", "farmacia", "regulacao"],
    "Educação": ["educacao", "escolar", "escola", "aluno", "merenda"],
    "Tributos": ["tribut", "arrecadacao", "iptu", "iss", "nota fiscal", "nfs-e", "divida ativa"],
    "Folha de pagamento": ["folha de pagamento", "recursos humanos", "rh "],
    "Contabilidade e finanças": ["contabil", "orcament", "tesouraria", "financeir", "compras", "licitac"],
    "Gestão integrada": ["gestao publica", "sistema integrado", "sistemas integrados", "erp", "gestao municipal"],
}
CATEGORIA_PADRAO = "Outros sistemas"

# Pontuação de prioridade (0 a 10) das oportunidades
PESOS = {
    "porte": 3.0,          # população do município (escala log)
    "valor": 3.0,          # valor estimado (escala log)
    "prazo": 2.0,          # disputa próxima ganha mais pontos
    "categoria": 2.0,      # categorias estratégicas
}
CATEGORIAS_ESTRATEGICAS = {"Gestão integrada", "Saúde", "Tributos"}

# Movimentações exibidas
DIAS_MOVIMENTACOES = 15

# Busca de um fornecedor no mapa (fornecedor_no_mapa.py)
# Raízes de CNPJ (8 primeiros dígitos, cobrem as filiais) do fornecedor a localizar; várias raízes cobrem um
# grupo econômico. Preencher aqui ou passar na linha de comando (--cnpj ... --nome ...).
FORNECEDOR_CNPJ_RAIZES: list[str] = []
FORNECEDOR_NOME = "Fornecedor monitorado"
FORNECEDOR_DESDE = "2023-01-01"     # início do histórico consultado
JANELA_HISTORICO_DIAS = 365         # período por consulta; se a API recusar, o script divide sozinho
BANCO_HISTORICO = RAIZ / "data" / "historico_contratos.db"


# --- Termos aditivos (prorrogação de vigência) ---
# A vigência que a busca e a API de consulta devolvem é a do contrato original; a prorrogação fica nos termos
# aditivos, consultados um contrato por vez (python main.py aditivos).
ADITIVOS_VENCIDOS_HA_DIAS = 1095   # consulta contratos de TI vencidos há até 3 anos...
ADITIVOS_VENCENDO_EM_DIAS = 90     # ...e os que vencem nos próximos 90 dias
ADITIVOS_REVISAR_DIAS = 30         # um contrato já consultado só é consultado de novo depois desse prazo

# Mensalidade estimada (valor ÷ meses): só para vigências dentro desta faixa
MENSALIDADE_MESES_MIN = 6
MENSALIDADE_MESES_MAX = 60


# --- BigQuery (publicação para o Looker Studio) ---
# Sandbox sem custo (10 GiB de armazenamento vitalício, 1 TiB/mês de consulta, tabelas expiram em
# 60 dias sem atualização). Requer um projeto no Google Cloud, a API BigQuery ativa e uma conta de
# serviço com as permissões BigQuery Data Editor + BigQuery Job User. A chave JSON fica FORA do Git
# (credenciais/ no .gitignore); o caminho é lido de GOOGLE_APPLICATION_CREDENTIALS ou desta linha.
BIGQUERY_PROJETO = "monitor-licitacoes-bq"   # ID do projeto no Google Cloud
BIGQUERY_DATASET = "licitacoes"              # dataset criado no console
BIGQUERY_LOCAL = "southamerica-east1"        # mesma região do dataset (informativo; o cliente usa o do projeto)
BIGQUERY_CAMINHO_CHAVE = str(RAIZ / "credenciais" / "bigquery-key.json")
# a variável GOOGLE_APPLICATION_CREDENTIALS, se definida, tem prioridade; "" = usa só a variável
BIGQUERY_TABELA_CONTROLE = "controle"        # tabela de controle (data/hora + contagens da última publicação)
# Se qualquer tabela publicada cair mais que este percentual vs. a última publicação, o publicar aborta
# (protege o painel do Looker de uma coleta incompleta ou de um filtro de TI quebrado). 0.20 = 20%.
BIGQUERY_QUEDA_MAXIMA = 0.20