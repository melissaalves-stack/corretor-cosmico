"""
═══════════════════════════════════════════════════════════════════════════
  CORRETOR CÓSMICO 🪐 — um agente autônomo na AWS
═══════════════════════════════════════════════════════════════════════════

  O que este código faz, todo dia, sem ninguém clicar em nada:

    1. ACORDA    → o EventBridge Scheduler invoca esta Lambda (cron diário)
    2. PESQUISA  → consulta o NASA Exoplanet Archive (API pública, sem chave)
    3. LEMBRA    → checa no DynamoDB quais planetas já anunciou (memória!)
    4. CALCULA   → distância em anos-luz e tempos de viagem reais
    5. ESCREVE   → o Amazon Nova (Bedrock) redige o anúncio imobiliário
    6. ENTREGA   → o Amazon SES envia o "Imóvel do Dia" por e-mail (HTML)

  Variáveis de ambiente:
    EMAIL_FROM   remetente verificado no SES              (obrigatória)
    EMAIL_TO     destinatário (verificado, se em sandbox) (obrigatória)
    MODEL_ID     modelo Bedrock    [padrão: amazon.nova-lite-v1:0]
    TABLE_NAME   tabela DynamoDB p/ memória               (opcional)
                 → sem ela, o agente funciona igual, só não "lembra"
                   dos planetas já anunciados. Degradação graciosa. 😉
═══════════════════════════════════════════════════════════════════════════
"""

import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

# ───────────────────────────────────────────────────────────────────────────
# CONSTANTES FÍSICAS E DE CONFIGURAÇÃO
# ───────────────────────────────────────────────────────────────────────────

#: Conversão oficial: 1 parsec = 3,26156 anos-luz
PARSEC_TO_LY = 3.26156

#: A Voyager 1 viaja a ~17 km/s. Um ano-luz tem ~9,46 trilhões de km.
#: Logo: 9,4607e12 km ÷ 17 km/s ÷ 31.557.600 s/ano ≈ 17.635 anos por ano-luz.
YEARS_PER_LY_VOYAGER = 17_635

#: A 10% da velocidade da luz (meta de projetos como o Breakthrough Starshot),
#: cada ano-luz leva exatos 10 anos. A matemática, às vezes, é gentil.
YEARS_PER_LY_AT_10PCT_C = 10

#: Endpoint TAP (Table Access Protocol) do NASA Exoplanet Archive.
#: TAP é um padrão astronômico: você manda SQL (dialeto ADQL), recebe JSON.
TAP_URL = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"

#: A consulta ADQL. A tabela `pscomppars` ("Planetary Systems Composite
#: Parameters") tem UMA linha por planeta confirmado, com os parâmetros
#: consolidados de várias publicações — perfeita para o nosso catálogo.
ADQL_QUERY = (
    "select pl_name, hostname, sy_dist, pl_rade, pl_bmasse, "
    "pl_orbper, pl_eqt, disc_year, discoverymethod, sy_snum, sy_pnum "
    "from pscomppars "
    "where sy_dist is not null and pl_rade is not null"
)


# ───────────────────────────────────────────────────────────────────────────
# ETAPA 1 · PESQUISAR — falar com a NASA
# ───────────────────────────────────────────────────────────────────────────

def fetch_planets(max_retries: int = 3) -> list[dict]:
    """Baixa o catálogo de exoplanetas confirmados (com retry exponencial).

    APIs públicas às vezes soluçam. Um agente autônomo não tem um humano
    por perto para apertar F5 — então ele mesmo tenta de novo: espera
    2s, depois 4s, depois 8s (backoff exponencial) antes de desistir.
    """
    params = urllib.parse.urlencode({"query": ADQL_QUERY, "format": "json"})
    req = urllib.request.Request(
        f"{TAP_URL}?{params}",
        headers={"User-Agent": "CorretorCosmico/2.0 (AWS Lambda; weekend challenge)"},
    )
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                catalog = json.loads(resp.read())
                print(f"[nasa] catálogo carregado: {len(catalog)} planetas confirmados")
                return catalog
        except Exception as err:  # noqa: BLE001 — queremos capturar tudo aqui
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            print(f"[nasa] tentativa {attempt} falhou ({err}); retry em {wait}s")
            time.sleep(wait)


# ───────────────────────────────────────────────────────────────────────────
# ETAPA 2 · LEMBRAR — a memória do agente (DynamoDB)
# ───────────────────────────────────────────────────────────────────────────
# Um agente sem memória anunciaria o mesmo planeta duas vezes — deselegante
# para um corretor. Guardamos cada planeta já anunciado como um item na
# tabela; a chave de partição é o próprio nome do planeta.

def _memory_table():
    """Retorna a tabela DynamoDB, ou None se a memória estiver desligada."""
    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        return None
    return boto3.resource("dynamodb").Table(table_name)


def already_listed(table, planet_name: str) -> bool:
    """O agente já anunciou este planeta antes?"""
    if table is None:
        return False
    resp = table.get_item(Key={"pl_name": planet_name})
    return "Item" in resp


def remember(table, planet_name: str) -> None:
    """Grava na memória que este planeta foi o imóvel do dia de hoje."""
    if table is None:
        return
    table.put_item(Item={
        "pl_name": planet_name,
        "listed_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"[memoria] '{planet_name}' registrado no DynamoDB")


def pick_unseen_planet(catalog: list[dict], table) -> dict:
    """Sorteia um planeta que AINDA NÃO foi anunciado.

    Estratégia simples e barata: sorteia e confere na memória. Com ~5.000
    planetas no catálogo e 1 anúncio/dia, colisões são raríssimas — mas o
    limite de 25 tentativas garante que nunca ficamos em loop infinito.
    """
    for _ in range(25):
        candidate = random.choice(catalog)
        if not already_listed(table, candidate["pl_name"]):
            return candidate
    return random.choice(catalog)  # catálogo quase esgotado? Aceita repetir.


# ───────────────────────────────────────────────────────────────────────────
# ETAPA 3 · CALCULAR — transformar dados brutos em fatos com significado
# ───────────────────────────────────────────────────────────────────────────

def build_facts(p: dict) -> dict:
    """Converte a linha crua do catálogo em fatos legíveis (e checáveis).

    Filosofia do projeto: a IA NÃO faz contas nem inventa números.
    Todo número que aparece no anúncio nasce aqui, em Python puro,
    onde dá para testar e auditar. A IA entra só na retórica.
    """
    dist_ly = round(p["sy_dist"] * PARSEC_TO_LY, 1)

    facts = {
        "nome": p.get("pl_name"),
        "estrela_hospedeira": p.get("hostname"),
        "sois_no_sistema": p.get("sy_snum"),          # imagine 2 pores do sol
        "planetas_vizinhos": p.get("sy_pnum"),
        "distancia_anos_luz": dist_ly,
        "raio_em_terras": p.get("pl_rade"),
        "massa_em_terras": p.get("pl_bmasse"),        # pode ser None — ok
        "duracao_do_ano_em_dias": p.get("pl_orbper"),
        "temperatura_de_equilibrio_K": p.get("pl_eqt"),
        "ano_da_descoberta": p.get("disc_year"),
        "metodo_de_descoberta": p.get("discoverymethod"),
        "viagem_na_velocidade_da_voyager_anos": int(dist_ly * YEARS_PER_LY_VOYAGER),
        "viagem_a_10pct_da_velocidade_da_luz_anos": int(dist_ly * YEARS_PER_LY_AT_10PCT_C),
    }

    # "Selo do corretor": um rótulo honesto e simplificado de habitabilidade.
    # Critério: temperatura de equilíbrio entre 180–310 K (água líquida
    # plausível com efeito estufa moderado) e raio ≤ 2 R⊕ (provável rochoso).
    # É uma heurística didática — a habitabilidade real depende de atmosfera,
    # composição e muito mais. O anúncio deixa isso claro.
    temp, raio = p.get("pl_eqt"), p.get("pl_rade")
    if temp and raio and 180 <= temp <= 310 and raio <= 2.0:
        facts["selo_do_corretor"] = (
            "🌡️ FAIXA INTRIGANTE: tamanho e temperatura compatíveis com um "
            "mundo rochoso e temperado (heurística simplificada!)"
        )
    return facts


# ───────────────────────────────────────────────────────────────────────────
# ETAPA 4 · ESCREVER — o Amazon Nova assume a persona
# ───────────────────────────────────────────────────────────────────────────

def write_listing(facts: dict) -> str:
    """Pede ao Nova (via API Converse do Bedrock) o anúncio do dia.

    Por que a API `converse`? Ela é unificada: o mesmo formato de chamada
    funciona para qualquer modelo do Bedrock. Trocar o Nova Lite pelo
    Nova Pro é mudar UMA variável de ambiente, zero código.
    """
    bedrock = boto3.client("bedrock-runtime")
    model_id = os.environ.get("MODEL_ID", "amazon.nova-lite-v1:0")

    prompt = f"""Você é o CORRETOR CÓSMICO, um corretor de imóveis intergaláctico
carismático, épico e bem-humorado. Escreva em português do Brasil o anúncio
do "IMÓVEL DO DIA" para o exoplaneta abaixo.

REGRA DE OURO: use SOMENTE os números fornecidos no JSON. Não invente,
não arredonde, não extrapole dados. Sua criatividade vai na retórica,
nunca nos fatos.

Estrutura do anúncio:
1. Título de anúncio irresistível (uma linha).
2. Parágrafo de venda épico: localização, vizinhança estelar, a "vista".
3. "FICHA TÉCNICA" em tópicos curtos com os dados reais.
4. "COMO CHEGAR": use os dois tempos de viagem calculados, com humor.
5. Feche com um aviso divertido de responsabilidade limitada interestelar.

Se existir o campo "selo_do_corretor", destaque-o — explicando que é uma
heurística simplificada. Máximo ~280 palavras.

Dados reais do NASA Exoplanet Archive:
{json.dumps(facts, ensure_ascii=False, indent=2)}"""

    resp = bedrock.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # temperature alta (0.9) = mais criatividade na PROSA.
        # Os números estão travados pelo prompt, então podemos ousar.
        inferenceConfig={"maxTokens": 800, "temperature": 0.9, "topP": 0.95},
    )
    return resp["output"]["message"]["content"][0]["text"]


# ───────────────────────────────────────────────────────────────────────────
# ETAPA 5 · ENTREGAR — e-mail bonito via SES (HTML + texto puro)
# ───────────────────────────────────────────────────────────────────────────

def send_email(subject: str, listing: str, facts: dict) -> None:
    """Envia o anúncio em duas versões: HTML (bonita) e texto (fallback).

    Clientes de e-mail antigos ou leitores de tela usam a versão texto —
    acessibilidade também é engenharia.
    """
    footer_txt = (
        "\n\n—\n🪐 Corretor Cósmico — enviado automaticamente por um agente "
        "AWS Lambda,\nagendado pelo EventBridge Scheduler, com texto do "
        "Amazon Nova (Bedrock)\ne memória em DynamoDB. "
        "Dados reais: NASA Exoplanet Archive.\nNenhum botão foi clicado."
    )

    # No HTML, preservamos as quebras de linha do texto do modelo com
    # white-space:pre-wrap — simples e robusto, sem parsear markdown.
    html = f"""\
<html><body style="margin:0;padding:0;background:#0b1120;">
  <div style="max-width:640px;margin:0 auto;padding:32px 24px;
              font-family:Georgia,serif;color:#e2e8f0;">
    <p style="font-size:13px;letter-spacing:2px;color:#818cf8;
              text-transform:uppercase;margin:0 0 4px;">Imóvel do dia</p>
    <h1 style="font-size:26px;color:#f8fafc;margin:0 0 20px;">
      🪐 {facts['nome']}</h1>
    <div style="white-space:pre-wrap;font-size:16px;line-height:1.6;">{listing}</div>
    <hr style="border:none;border-top:1px solid #334155;margin:28px 0;">
    <p style="font-size:12px;color:#64748b;line-height:1.6;">
      Enviado automaticamente por um agente <b>AWS Lambda</b> agendado pelo
      <b>EventBridge Scheduler</b>, com texto do <b>Amazon Nova</b> (Bedrock)
      e memória em <b>DynamoDB</b>. Dados reais do NASA Exoplanet Archive.<br>
      <i>Nenhum botão foi clicado na produção deste e-mail.</i>
    </p>
  </div>
</body></html>"""

    boto3.client("ses").send_email(
        Source=os.environ["EMAIL_FROM"],
        Destination={"ToAddresses": [os.environ["EMAIL_TO"]]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": listing + footer_txt, "Charset": "UTF-8"},
                "Html": {"Data": html, "Charset": "UTF-8"},
            },
        },
    )


# ───────────────────────────────────────────────────────────────────────────
# O MAESTRO — handler chamado pelo EventBridge Scheduler
# ───────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    """Orquestra o dia de trabalho do corretor, do catálogo à caixa de entrada."""
    t0 = time.time()

    catalog = fetch_planets()                       # 1. pesquisa
    memory = _memory_table()                        # 2. abre a memória
    planet = pick_unseen_planet(catalog, memory)    #    e evita repeteco
    facts = build_facts(planet)                     # 3. calcula
    listing = write_listing(facts)                  # 4. escreve
    send_email(f"🪐 Imóvel do dia: {facts['nome']}", listing, facts)  # 5. entrega
    remember(memory, facts["nome"])                 # 6. anota no caderninho

    # Log estruturado (JSON): fácil de filtrar no CloudWatch Logs Insights.
    summary = {
        "planeta": facts["nome"],
        "distancia_anos_luz": facts["distancia_anos_luz"],
        "memoria_ativa": memory is not None,
        "duracao_segundos": round(time.time() - t0, 1),
        "status": "email_enviado",
    }
    print(json.dumps(summary, ensure_ascii=False))
    return {"statusCode": 200, **summary}
