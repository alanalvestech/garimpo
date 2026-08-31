"""Lê o último arquivo de cada fonte, traduz com o Gemini e grava em data/.

Cada categoria tem sua pasta, e a pasta guarda só o dia mais recente que a
fonte publicou:

    data/<Categoria>/AAAA-MM-DD.md
    data/<Categoria>/AAAA-MM-DD.json

A data no nome é a do arquivo de origem, não a do dia em que a coleta rodou:
fonte atrasada aparece com a data dela. Quando um dia novo entra, o anterior
sai da pasta e fica só no histórico do git. Dia já gravado é pulado, então
rodar de novo não regrava nem gasta chamada.

Uso:
    GEMINI_API_KEY=... python scripts/collect.py

Variáveis:
    GEMINI_API_KEY  obrigatória
    GEMINI_MODEL    padrão gemini-2.5-flash
    GITHUB_TOKEN    opcional, só para elevar o limite da API do GitHub
    RADAR_FORCE     "1" regrava o dia que já existe
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
DIR_DADOS = RAIZ / "data"
MODELO = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FORCAR = os.environ.get("RADAR_FORCE") == "1"
LIMITE_CARACTERES = 12000  # corta arquivo gigante antes de mandar pro modelo

PROMPT = """Você recebe o conteúdo bruto de um arquivo diário de um agregador de notícias de tecnologia, escrito em inglês ou chinês.

Traduza e resuma para português do Brasil, seguindo estas regras:

- Uma linha por item, no formato: **título** seguido do link, e abaixo um resumo de no máximo duas frases.
- Mantenha os links originais intactos.
- Descarte itens sem link, propaganda, rodapé, sumário e índice.
- Não invente informação que não está no texto.
- Não use travessão em nenhuma hipótese.
- Se o arquivo não tiver nada aproveitável, responda exatamente: SEM CONTEUDO

Conteúdo:

---
{conteudo}
---
"""


def http_json(url):
    req = urllib.request.Request(url, headers=cabecalhos())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


CACHE_TEXTO = {}  # o mesmo arquivo alimenta várias categorias, baixa uma vez só


def http_texto(url):
    if url not in CACHE_TEXTO:
        req = urllib.request.Request(url, headers=cabecalhos())
        with urllib.request.urlopen(req, timeout=60) as r:
            CACHE_TEXTO[url] = r.read().decode("utf-8", errors="replace")
    return CACHE_TEXTO[url]


def cabecalhos():
    h = {"User-Agent": "radar", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def ancora(titulo):
    """Reproduz a âncora que o GitHub gera para um título de seção."""
    limpo = "".join(c for c in titulo.lower() if c.isalnum() or c in " -_")
    return "#" + limpo.strip("_").replace(" ", "-")


def recortar_secao(texto, secao):
    """Devolve (titulo, corpo) do bloco cujo título contém `secao`, ou None."""
    linhas = texto.splitlines()
    inicio = None
    for i, linha in enumerate(linhas):
        if linha.startswith("## "):
            if inicio is not None:
                return linhas[inicio], "\n".join(linhas[inicio + 1 : i]).strip()
            if secao.lower() in linha.lower():
                inicio = i
    if inicio is None:
        return None
    return linhas[inicio], "\n".join(linhas[inicio + 1 :]).strip()


def data_no_nome(nome):
    """Extrai uma data de nomes tipo 2026-08-31.md ou 20260831.md."""
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", nome)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def ultimo_arquivo(fonte):
    """Devolve o arquivo com a data mais recente na pasta da fonte."""
    caminho = fonte["path"].strip("./")
    url = f"https://api.github.com/repos/{fonte['repo']}/contents/{caminho}"
    try:
        itens = http_json(url)
    except urllib.error.HTTPError as e:
        print(f"  [erro] {fonte['repo']}/{caminho}: HTTP {e.code}", file=sys.stderr)
        return None

    if not isinstance(itens, list):
        return None

    achados = []
    for item in itens:
        if item.get("type") != "file":
            continue
        nome = item["name"]
        if not any(nome.endswith(e) for e in fonte.get("ext", [".md"])):
            continue
        d = data_no_nome(nome)
        if d is None:
            continue
        achados.append(
            {
                "nome": nome,
                "data": d,
                "download_url": item["download_url"],
                "html_url": item["html_url"],
            }
        )
    if not achados:
        return None
    return max(achados, key=lambda a: a["data"])


def traduzir(conteudo):
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODELO}:generateContent"
    )
    corpo = {
        "contents": [
            {"parts": [{"text": PROMPT.format(conteudo=conteudo[:LIMITE_CARACTERES])}]}
        ],
        "generationConfig": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(corpo).encode(),
        headers={
            "Content-Type": "application/json",
            # No cabeçalho, não na query: URL com chave vaza em mensagem de erro,
            # traceback e log de proxy.
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resposta = json.load(r)
    try:
        return resposta["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        print(f"  [erro] resposta inesperada do Gemini: {resposta}", file=sys.stderr)
        return ""


def gravar(fonte, arq, texto, agora, pendente=False):
    """Grava o par .md/.json da fonte na data do arquivo de origem."""
    dia = arq["data"].isoformat()
    pasta = DIR_DADOS / fonte["category"]
    pasta.mkdir(parents=True, exist_ok=True)
    url = arq["html_url"]
    if arq.get("titulo_secao"):
        url += ancora(arq["titulo_secao"])

    registro = {
        "categoria": fonte["category"],
        "fonte": fonte["name"],
        "repo": fonte["repo"],
        "arquivo": arq["nome"],
        "secao": fonte.get("section"),
        "data": dia,
        "modo": fonte["mode"],
        "licenca": fonte["license"],
        "url": url,
        "gerado_em": agora,
        "pendente": pendente,
        "conteudo": texto,
    }
    (pasta / f"{dia}.json").write_text(
        json.dumps(registro, ensure_ascii=False, indent=2)
    )

    origem = arq["nome"]
    if fonte.get("section"):
        origem += f" · bloco {fonte['section']}"
    cabecalho = (
        f"# {fonte['name']} · {dia}\n\n"
        f"Origem: [{origem}]({url}) · licença {fonte['license']}\n\n"
    )
    if texto:
        corpo = texto
    elif pendente:
        corpo = (
            "Tradução pendente. O conteúdo entra na próxima coleta que rodar "
            "com o GEMINI_API_KEY definido."
        )
    else:
        corpo = (
            "Sem licença de redistribuição, então aqui fica só o ponteiro "
            "para o original."
        )
    (pasta / f"{dia}.md").write_text(cabecalho + corpo + "\n")
    print(f"  escrito data/{fonte['category']}/{dia}.md")


def main():
    tem_chave = bool(os.environ.get("GEMINI_API_KEY"))
    if not tem_chave:
        # Sem chave a fonte com licença fica registrada como ponteiro, marcada
        # como pendente, e a próxima coleta com chave regrava com o conteúdo.
        print("GEMINI_API_KEY não definida: gravando só ponteiro", file=sys.stderr)

    fontes = yaml.safe_load((RAIZ / "config" / "sources.yaml").read_text())["sources"]
    categorias = [f["category"] for f in fontes]
    repetidas = {c for c in categorias if categorias.count(c) > 1}
    if repetidas:
        # Duas fontes na mesma categoria sobrescreveriam o arquivo uma da outra.
        sys.exit(f"categoria repetida em sources.yaml: {', '.join(sorted(repetidas))}")

    agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    escritos = 0
    falhas = 0

    for fonte in fontes:
        print(f"[{fonte['category']}] {fonte['repo']}/{fonte['path']}")
        arq = ultimo_arquivo(fonte)
        if arq is None:
            print("  nenhum arquivo com data no nome")
            continue

        dia = arq["data"].isoformat()
        pasta = DIR_DADOS / fonte["category"]
        destino = pasta / f"{dia}.json"
        if destino.exists() and not FORCAR and not repescar(destino, tem_chave):
            print(f"  {arq['nome']}: já gravado")
            limpar(pasta, dia)
            continue

        print(f"  {arq['nome']}")
        try:
            if not processar(fonte, arq, agora, tem_chave):
                continue
        except Exception as e:
            # Falha numa fonte não pode derrubar as outras, senão uma cota
            # estourada no meio do caminho perde a coleta do dia inteiro.
            print(f"  [erro] {arq['nome']}: {e}", file=sys.stderr)
            falhas += 1
            continue
        limpar(pasta, dia)
        escritos += 1

    print(f"{escritos} arquivos novos" if escritos else "nada novo")
    if falhas:
        print(f"{falhas} arquivo(s) falharam", file=sys.stderr)


def limpar(pasta, dia):
    """Deixa na pasta só o dia atual: o anterior fica no histórico do git."""
    for velho in pasta.glob("*.*"):
        if velho.stem != dia:
            velho.unlink()
            print(f"  removido {velho.relative_to(RAIZ)}")


def repescar(destino, tem_chave):
    """Diz se um dia já gravado deve ser refeito, por ter ficado pendente."""
    if not tem_chave:
        return False
    try:
        return json.loads(destino.read_text()).get("pendente", False)
    except (json.JSONDecodeError, OSError):
        return False


def processar(fonte, arq, agora, tem_chave):
    """Baixa, recorta o bloco se houver, traduz se a licença permitir, e grava.

    Devolve False quando não há o que gravar.
    """
    bruto = None
    if fonte.get("section") or (fonte["mode"] == "full" and tem_chave):
        bruto = http_texto(arq["download_url"])

    if fonte.get("section"):
        # A fonte junta vários blocos num arquivo só, e cada bloco vira uma
        # categoria. Sem o bloco, não há o que gravar.
        recorte = recortar_secao(bruto, fonte["section"])
        if recorte is None:
            print(f"  sem o bloco {fonte['section']}")
            return False
        arq["titulo_secao"] = recorte[0].lstrip("# ")
        bruto = f"{recorte[0]}\n\n{recorte[1]}"

    if fonte["mode"] == "full" and not tem_chave:
        gravar(fonte, arq, None, agora, pendente=True)
        return True

    texto = None
    if fonte["mode"] == "full":
        texto = traduzir(bruto)
        if not texto or texto.strip() == "SEM CONTEUDO":
            print("  nada aproveitável")
            return False

    gravar(fonte, arq, texto, agora)
    return True


if __name__ == "__main__":
    main()
