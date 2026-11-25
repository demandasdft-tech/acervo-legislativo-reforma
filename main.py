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
# 1. VALIDAÇÃO DE CONFIGURAÇÃO E AMBIENTE
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GCP_SA_CREDENTIALS = os.getenv("GCP_SA_CREDENTIALS")

DOC_ID_CGIBS = os.getenv("DOC_ID_CGIBS")
DOC_ID_NFSE = os.getenv("DOC_ID_NFSE")
DOC_ID_RFB = os.getenv("DOC_ID_RFB")

if not all([GEMINI_API_KEY, GCP_SA_CREDENTIALS, DOC_ID_CGIBS, DOC_ID_NFSE, DOC_ID_RFB]):
    print("ERRO CRÍTICO: Faltam variáveis de ambiente cadastradas nos Secrets do repositório.")
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)
MODELO_IA = "gemini-3.5-flash-lite"
ARQUIVO_HISTORICO = "atos_ingeridos.json"

# ==========================================
# 2. CONTROLE DE ATOS JÁ PROCESSADOS (MEMÓRIA)
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

# ==========================================
# 3. BUSCA DE PUBLICAÇÕES NORMATIVAS OFICIAIS
# ==========================================
FEEDS_NORMATIVOS = [
    # DOU Seção 1 - Órgãos da Reforma Tributária
    "https://news.google.com/rss/search?q=site:in.gov.br+(\"Comitê+Gestor+do+IBS\"+OR+\"Comitê+Gestor+da+Nota+Fiscal\"+OR+\"Receita+Federal\"+AND+\"IBS\")+when:1y&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    # Publicações de atos, resoluções e instruções normativas no gov.br
    "https://news.google.com/rss/search?q=(\"Resolução\"+OR+\"Ato+Conjunto\"+OR+\"Instrução+Normativa\")+AND+(\"IBS\"+OR+\"CBS\"+OR+\"CGIBS\")+site:gov.br+when:1y&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    # Notas Técnicas e orientações da ABRASF e CNM
    "https://news.google.com/rss/search?q=(\"Nota+Técnica\"+OR+\"Parecer\")+AND+(\"ABRASF\"+OR+\"CNM\")+AND+(\"Reforma+Tributária\"+OR+\"IBS\")+when:1y&hl=pt-BR&gl=BR&ceid=BR:pt-419"
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
            print(f"Aviso ao ler feed normativo {url}: {e}")

    # Remove duplicados por título
    unicos = []
    vistos = set()
    for p in publicacoes:
        if p['titulo'] not in vistos:
            vistos.add(p['titulo'])
            unicos.append(p)
    return unicos

def extrair_texto_completo(url):
    """Extrai o texto na íntegra da publicação oficial"""
    try:
        baixado = trafilatura.fetch_url(url)
        if baixado:
            texto = trafilatura.extract(baixado, include_tables=True)
            if texto and len(texto.strip()) > 200:
                return texto[:12000]
    except Exception:
        pass

    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            paragrafos = [p.get_text().strip() for p in soup.find_all("p")]
            return "\n".join([p for p in paragrafos if len(p) > 30])[:12000]
    except Exception:
        pass
    return ""

# ==========================================
# 4. CLASSIFICAÇÃO E FORMATAÇÃO COM IA
# ==========================================
def classificar_e_estruturar_ato(titulo, texto_completo):
    prompt = f"""
Você é um Auditor Fiscal Especialista em Legislação Tributária Municipal.

Analise a publicação abaixo e determine com rigor:
1. Trata-se de uma NORMA OFICIAL, RESOLUÇÃO, DELIBERAÇÃO, INSTRUÇÃO NORMATIVA, ATO CONJUNTO ou NOTA TÉCNICA OFICIAL com aplicabilidade ou reflexo na Reforma Tributária / Municípios / ISS / IBS?
(Se for apenas notícia jornalística, artigo de opinião ou matéria genérica sem caráter normativo, responda apenas: CLASSIFICACAO: DESCARTAR)

2. Se for uma norma ou ato oficial, determine o destino exato:
- Se for do Comitê Gestor do IBS (CGIBS): CLASSIFICACAO: CGIBS
- Se for do Comitê Gestor da NFS-e (CGNFS-e) ou padrões de nota técnica de NFS-e: CLASSIFICACAO: NFSE
- Se for Instrução Normativa da Receita Federal (RFB), Ato Conjunto RFB/PGFN ou Nota Técnica de entidade municipalista (ABRASF/CNM): CLASSIFICACAO: RFB_ENTIDADES

3. Se classificado, formate a entrada para ser anexada ao Compêndio Jurídico oficial no seguinte padrão rigoroso:

---
### [TIPO E NÚMERO DO ATO - EX: RESOLUÇÃO CGIBS Nº X, DE DD/MM/AAAA]
- **Órgão Emissor:** [Ex: Comitê Gestor do IBS]
- **Ementa Oficial:** [Ementa ou síntese do objetivo da norma]
- **Data de Publicação / Vigência:** [Datas identificadas]
- **Impacto para a Fiscalização de Jundiaí:** [1 parágrafo explicando as providências exigidas da Prefeitura]

#### TEXTO INTEGRAL / DISPOSITIVOS PRINCIPAIS:
[Transcreva fielmente os artigos, incisos e dispositivos principais da norma]

---

Título da matéria: {titulo}
Texto extraído:
{texto_completo[:9000]}
"""
    model = genai.GenerativeModel(MODELO_IA)
    resp = model.generate_content(prompt).text
    return resp

# ==========================================
# 5. INJEÇÃO NO GOOGLE DOC RESPECTIVO
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
# FLUXO PRINCIPAL
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
            continue # Já avaliado anteriormente

        print(f"Avaliando: {pub['titulo'][:60]}...")
        texto = extrair_texto_completo(pub['link'])
        if not texto or len(texto) < 150:
            continue

        analise = classificar_e_estruturar_ato(pub['titulo'], texto)

        if "CLASSIFICACAO: DESCARTAR" in analise:
            historico.append(identificador)
            continue

        destino_doc = None
        if "CLASSIFICACAO: CGIBS" in analise:
            destino_doc = DOC_ID_CGIBS
            print(" -> Classificado como ato do CGIBS (Doc 02)")
        elif "CLASSIFICACAO: NFSE" in analise:
            destino_doc = DOC_ID_NFSE
            print(" -> Classificado como ato da NFS-e Nacional (Doc 03)")
        elif "CLASSIFICACAO: RFB_ENTIDADES" in analise:
            destino_doc = DOC_ID_RFB
            print(" -> Classificado como RFB / Municipalista (Doc 04)")

        if destino_doc:
            # Remove as tags de instrução da IA para deixar o texto limpo no documento
            texto_limpo = analise.replace("CLASSIFICACAO: CGIBS", "").replace("CLASSIFICACAO: NFSE", "").replace("CLASSIFICACAO: RFB_ENTIDADES", "").strip()
            sucesso = anexar_ato_no_doc(destino_doc, texto_limpo)
            if sucesso:
                print(" ✅ Ato inserido com sucesso na base normativa do Google Drive!")
                novos_ingeridos += 1

        historico.append(identificador)

    salvar_historico(historico)
    print(f"Varredura finalizada. Novos atos oficiais ingeridos hoje: {novos_ingeridos}")
