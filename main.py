import os
import sys
import json
import datetime
from urllib.parse import urlparse
from dateutil import parser
import feedparser
import requests
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
import trafilatura
from bs4 import BeautifulSoup

# ==========================================
# 1. VALIDAÇÃO DE AMBIENTE
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GCP_SA_CREDENTIALS = os.getenv("GCP_SA_CREDENTIALS")

DOC_ID_CGIBS = os.getenv("DOC_ID_CGIBS")
DOC_ID_NFSE = os.getenv("DOC_ID_NFSE")
DOC_ID_RFB = os.getenv("DOC_ID_RFB")

if not all([GEMINI_API_KEY, GCP_SA_CREDENTIALS, DOC_ID_CGIBS, DOC_ID_NFSE, DOC_ID_RFB]):
    print("ERRO: Faltam variáveis de ambiente nos Secrets.")
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)
MODELO_IA = "gemini-3.5-flash-lite"
ARQUIVO_HISTORICO = "atos_ingeridos.json"

# ==========================================
# 2. RESOLVEDOR DE LINKS REAIS
# ==========================================
def resolver_url_real(google_url):
    """Resolve o redirecionamento do Google News para pegar a página real do DOU/portal"""
    if "news.google.com" not in google_url:
        return google_url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    }

    try:
        resp = requests.get(google_url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        cwiz = soup.select_one('c-wiz[data-p]')
        if cwiz and cwiz.get('data-p'):
            data_p = cwiz.get('data-p')
            obj = json.loads(data_p.replace('%.@.', '["garturlreq",'))
            payload = {'f.req': json.dumps([[["Fbv4je", json.dumps(obj[:-6] + obj[-2:]), "null", "generic"]]])}
            p_headers = {
                'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'User-Agent': headers["User-Agent"]
            }
            post_resp = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", headers=p_headers, data=payload, timeout=10)
            array_str = json.loads(post_resp.text.replace(")]}'", ""))[0][2]
            url_resolvida = json.loads(array_str)[1]
            if urlparse(url_resolvida).path not in ["", "/"]:
                return url_resolvida
    except Exception:
        pass

    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(google_url)
        if isinstance(res, dict) and res.get("decoded_url"):
            decoded = res["decoded_url"]
            if urlparse(decoded).path not in ["", "/"]:
                return decoded
    except Exception:
        pass

    return google_url

def extrair_texto_completo(url_alvo):
    url_real = resolver_url_real(url_alvo)
    texto = ""

    try:
        baixado = trafilatura.fetch_url(url_real)
        if baixado:
            texto = trafilatura.extract(baixado, include_tables=True) or ""
    except Exception:
        pass

    if not texto or len(texto.strip()) < 150:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url_real, headers=headers, timeout=12)
            if resp.ok:
                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                paragrafos = [p.get_text().strip() for p in soup.find_all("p")]
                texto = "\n".join([p for p in paragrafos if len(p) > 30])
        except Exception:
            pass

    return url_real, texto[:12000]

# ==========================================
# 3. FEEDS E HISTÓRICO
# ==========================================
def carregar_historico():
    if os.path.exists(ARQUIVO_HISTORICO):
        try:
            with open(ARQUIVO_HISTORICO, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def salvar_historico(historico):
    with open(ARQUIVO_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)

FEEDS_NORMATIVOS = [
    # DOU Seção 1 - Atos e resoluções
    "https://news.google.com/rss/search?q=site:in.gov.br+(\"Comitê+Gestor\"+OR+\"IBS\"+OR+\"NFS-e\"+OR+\"Reforma+Tributária\")+when:1y&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    # Normas, Resoluções e Instruções Normativas oficiais
    "https://news.google.com/rss/search?q=(\"Resolução\"+OR+\"Instrução+Normativa\"+OR+\"Ato+Declaratório\")+AND+(\"IBS\"+OR+\"CBS\"+OR+\"NFS-e\")+site:gov.br+when:1y&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    # Notas Técnicas ABRASF e CNM
    "https://news.google.com/rss/search?q=(\"Nota+Técnica\"+OR+\"Parecer\")+AND+(\"ABRASF\"+OR+\"CNM\")+AND+(\"Reforma+Tributária\"+OR+\"ISS\"+OR+\"IBS\")+when:1y&hl=pt-BR&gl=BR&ceid=BR:pt-419"
]

def coletar_publicacoes_normativas():
    publicacoes = []
    for url in FEEDS_NORMATIVOS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                publicacoes.append({
                    "titulo": entry.title,
                    "link": entry.link,
                    "sumario": entry.get("summary", "")[:400]
                })
        except Exception as e:
            print(f"Aviso no feed {url}: {e}")

    unicos = []
    vistos = set()
    for p in publicacoes:
        if p['titulo'] not in vistos:
            vistos.add(p['titulo'])
            unicos.append(p)
    return unicos

# ==========================================
# 4. CLASSIFICAÇÃO COM IA
# ==========================================
def classificar_e_estruturar_ato(titulo, texto_completo, sumario_feed):
    conteudo_analise = texto_completo if len(texto_completo) > 200 else sumario_feed

    prompt = f"""
Você é um Auditor Fiscal Especialista em Legislação Tributária Municipal.

Analise o texto abaixo e determine:
1. Este conteúdo trata de uma NORMA OFICIAL, RESOLUÇÃO, DELIBERAÇÃO, INSTRUÇÃO NORMATIVA, ATO CONJUNTO, CONVÊNIO ou NOTA TÉCNICA FORMAL que oriente ou afete a Reforma Tributária / Municípios / ISS / IBS / NFS-e?
(Se for apenas notícia de jornal, artigo opinativo sem caráter normativo oficial ou conteúdo comercial, responda apenas: CLASSIFICACAO: DESCARTAR)

2. Se for norma, ato ou orientação técnica relevante, determine o destino:
- Se for do Comitê Gestor do IBS (CGIBS): CLASSIFICACAO: CGIBS
- Se for do Comitê Gestor da NFS-e (CGNFS-e) ou padrão técnico de nota fiscal: CLASSIFICACAO: NFSE
- Se for Instrução Normativa da RFB, Portaria ou Nota Técnica oficial (ABRASF/CNM): CLASSIFICACAO: RFB_ENTIDADES

3. Se classificado, formate a publicação para o Compêndio Jurídico oficial:

---
### [TIPO E NÚMERO DO ATO OU NOTA TÉCNICA]
- **Órgão Emissor:** [Ex: Comitê Gestor do IBS / RFB / ABRASF]
- **Ementa / Objeto:** [Síntese do objetivo]
- **Data / Vigência:** [Datas identificadas]
- **Impacto para a Fiscalização de Jundiaí:** [1 parágrafo explicando as providências exigidas]

#### DISPOSITIVOS PRINCIPAIS / TEOR DA ORIENTAÇÃO:
[Transcreva os artigos, orientações e determinações principais fielmente]

---

Título: {titulo}
Texto:
{conteudo_analise[:9000]}
"""
    model = genai.GenerativeModel(MODELO_IA)
    return model.generate_content(prompt).text

# ==========================================
# 5. INJEÇÃO NO GOOGLE DOC
# ==========================================
def anexar_ato_no_doc(doc_id, texto_formatado):
    try:
        creds_dict = json.loads(GCP_SA_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/documents']
        )
        docs_service = build('docs', 'v1', credentials=creds)

        doc = docs_service.documents().get(documentId=doc_id).execute()
        fim_indice = max(1, doc.get('body').get('content')[-1].get('endIndex') - 1)

        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': [{
                'insertText': {
                    'location': {'index': fim_indice},
                    'text': f"\n\n{texto_formatado}\n"
                }
            }]}
        ).execute()
        return True
    except Exception as e:
        print(f"Erro ao gravar no Google Doc ({doc_id}): {e}")
        return False

# ==========================================
# EXECUÇÃO PRINCIPAL
# ==========================================
if __name__ == "__main__":
    print("Iniciando varredura normativa de atos oficiais...")
    historico = carregar_historico()
    publicacoes = coletar_publicacoes_normativas()
    print(f"Publicações identificadas nas fontes: {len(publicacoes)}")

    novos_ingeridos = 0

    for pub in publicacoes:
        identificador = pub['titulo'].strip().lower()
        if identificador in historico:
            continue

        print(f"\n-> Avaliando: {pub['titulo'][:65]}...")
        url_real, texto = extrair_conteudo_pagina(pub['link'])
        
        analise = classificar_e_estruturar_ato(pub['titulo'], texto, pub['sumario'])

        if "CLASSIFICACAO: DESCARTAR" in analise:
            print("   [Descartado: Não é ato normativo/resolução oficial]")
            historico.append(identificador)
            continue

        destino_doc = None
        if "CLASSIFICACAO: CGIBS" in analise:
            destino_doc = DOC_ID_CGIBS
            print("   [Aceito -> Doc 02: Comitê Gestor do IBS]")
        elif "CLASSIFICACAO: NFSE" in analise:
            destino_doc = DOC_ID_NFSE
            print("   [Aceito -> Doc 03: NFS-e Nacional]")
        elif "CLASSIFICACAO: RFB_ENTIDADES" in analise:
            destino_doc = DOC_ID_RFB
            print("   [Aceito -> Doc 04: RFB / Entidades]")

        if destino_doc:
            texto_limpo = analise.replace("CLASSIFICACAO: CGIBS", "").replace("CLASSIFICACAO: NFSE", "").replace("CLASSIFICACAO: RFB_ENTIDADES", "").strip()
            sucesso = anexar_ato_no_doc(destino_doc, texto_limpo)
            if sucesso:
                print("   ✅ Gravado com sucesso no Google Doc correspondente!")
                novos_ingeridos += 1

        historico.append(identificador)

    salvar_historico(historico)
    print(f"\n=======================================================")
    print(f"Varredura concluída. Novos atos ingeridos com sucesso: {novos_ingeridos}")
    print(f"=======================================================")
