"""Lê os arquivos mais recentes das fontes, traduz com o Gemini e grava em data/.

Cada categoria tem sua pasta, e cada dia vira um par de arquivos:

    data/<Categoria>/AAAA-MM-DD.md
    data/<Categoria>/AAAA-MM-DD.json

A data no nome é a do arquivo de origem, não a do dia em que a coleta rodou.
Dia já gravado é pulado, então rodar de novo não regrava nem gasta chamada.

Uso:
    GEMINI_API_KEY=... python scripts/collect.py

Variáveis:
    GEMINI_API_KEY  obrigatória
    GEMINI_MODEL    padrão gemini-2.5-flash
    GITHUB_TOKEN    opcional, só para elevar o limite da API do GitHub
    RADAR_DAYS      quantos dias para trás considerar um arquivo recente (padrão 2)
    RADAR_FORCE     "1" regrava dias que já existem
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
DIR_DADOS = RAIZ / "data"
MODELO = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DIAS = int(os.environ.get("RADAR_DAYS", "2"))
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


def http_texto(url):
    req = urllib.request.Request(url, headers=cabecalhos())
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def cabecalhos():
    h = {"User-Agent": "radar", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def data_no_nome(nome):
    """Extrai uma data de nomes tipo 2026-08-31.md ou 20260831.md."""
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", nome)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def arquivos_recentes(fonte, corte):
    """Lista arquivos da pasta da fonte cuja data no nome é >= corte."""
    caminho = fonte["path"].strip("./")
    url = f"https://api.github.com/repos/{fonte['repo']}/contents/{caminho}"
    try:
        itens = http_json(url)
    except urllib.error.HTTPError as e:
        print(f"  [erro] {fonte['repo']}/{caminho}: HTTP {e.code}", file=sys.stderr)
        return []

    if not isinstance(itens, list):
        return []

    achados = []
    for item in itens:
        if item.get("type") != "file":
            continue
        nome = item["name"]
        if not any(nome.endswith(e) for e in fonte.get("ext", [".md"])):
            continue
        d = data_no_nome(nome)
        if d is None or d < corte:
            continue
        achados.append(
            {
                "nome": nome,
                "data": d,
                "download_url": item["download_url"],
                "html_url": item["html_url"],
            }
        )
    achados.sort(key=lambda a: a["data"], reverse=True)
    return achados


def traduzir(conteudo):
    chave = os.environ["GEMINI_API_KEY"]
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODELO}:generateContent?key={chave}"
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
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resposta = json.load(r)
    try:
        return resposta["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        print(f"  [erro] resposta inesperada do Gemini: {resposta}", file=sys.stderr)
        return ""


def gravar(fonte, arq, texto, agora):
    """Grava o par .md/.json da fonte na data do arquivo de origem."""
    dia = arq["data"].isoformat()
    pasta = DIR_DADOS / fonte["category"]
    pasta.mkdir(parents=True, exist_ok=True)

    registro = {
        "categoria": fonte["category"],
        "fonte": fonte["name"],
        "repo": fonte["repo"],
        "arquivo": arq["nome"],
        "data": dia,
        "modo": fonte["mode"],
        "licenca": fonte["license"],
        "url": arq["html_url"],
        "gerado_em": agora,
        "conteudo": texto,
    }
    (pasta / f"{dia}.json").write_text(
        json.dumps(registro, ensure_ascii=False, indent=2)
    )

    cabecalho = (
        f"# {fonte['name']} · {dia}\n\n"
        f"Origem: [{arq['nome']}]({arq['html_url']}) · licença {fonte['license']}\n\n"
    )
    corpo = texto or (
        "Sem licença de redistribuição, então aqui fica só o ponteiro para o original."
    )
    (pasta / f"{dia}.md").write_text(cabecalho + corpo + "\n")
    print(f"  escrito data/{fonte['category']}/{dia}.md")


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY não definida")

    fontes = yaml.safe_load((RAIZ / "config" / "sources.yaml").read_text())["sources"]
    categorias = [f["category"] for f in fontes]
    repetidas = {c for c in categorias if categorias.count(c) > 1}
    if repetidas:
        # Duas fontes na mesma categoria sobrescreveriam o arquivo uma da outra.
        sys.exit(f"categoria repetida em sources.yaml: {', '.join(sorted(repetidas))}")

    corte = date.today() - timedelta(days=DIAS)
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    escritos = 0

    for fonte in fontes:
        print(f"[{fonte['name']}] {fonte['repo']}")
        for arq in arquivos_recentes(fonte, corte):
            dia = arq["data"].isoformat()
            destino = DIR_DADOS / fonte["category"] / f"{dia}.json"
            if destino.exists() and not FORCAR:
                print(f"  {arq['nome']}: já gravado")
                continue

            print(f"  {arq['nome']}")
            texto = None
            if fonte["mode"] == "full":
                texto = traduzir(http_texto(arq["download_url"]))
                if not texto or texto.strip() == "SEM CONTEUDO":
                    print("  nada aproveitável")
                    continue

            gravar(fonte, arq, texto, agora)
            escritos += 1

    print("nada novo" if not escritos else f"{escritos} arquivos novos")


if __name__ == "__main__":
    main()
