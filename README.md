# radar

Notícias diárias de tecnologia, em português, reunidas de agregadores públicos do GitHub.

Todo dia às 07:00 (horário de Brasília) uma rotina lê os arquivos mais recentes das fontes listadas em [`sources.yaml`](sources.yaml), traduz e resume com o Gemini, e grava dois arquivos em [`data/`](data):

- `AAAA-MM-DD.md` para ler
- `AAAA-MM-DD.json` para consultar

## Consultar

O JSON é regular, então dá para perguntar direto ao histórico sem baixar nada além do repositório:

```sql
-- o que apareceu nos últimos 7 dias, por fonte
SELECT data, fonte, arquivo
FROM read_json_auto('data/*.json', union_by_name=true),
     UNNEST(itens) AS t(item)
WHERE data >= current_date - 7
ORDER BY data DESC;
```

```bash
duckdb -c "SELECT * FROM read_json_auto('data/*.json')"
```

## Fontes e direitos

Cada fonte declara em `sources.yaml` o modo de uso, e o modo segue a licença do repositório de origem:

- **full**: o conteúdo é traduzido e republicado aqui, com atribuição e link para o original. Usado apenas quando a licença permite (hoje, MIT).
- **link**: o repositório de origem não declara licença, então aqui fica apenas o ponteiro para o arquivo original.

Os direitos das matérias originais são de seus veículos. Este repositório reúne referências, links e resumos, e não substitui a leitura na fonte.

## Rodar na mão

```bash
pip install pyyaml
GEMINI_API_KEY=sua_chave python scripts/collect.py
```

Variáveis aceitas: `GEMINI_API_KEY` (obrigatória), `GEMINI_MODEL` (padrão `gemini-2.5-flash`), `RADAR_DAYS` (quantos dias para trás contam como recente, padrão 2) e `GITHUB_TOKEN` (opcional, eleva o limite da API do GitHub).

## Licença

O código deste repositório está sob MIT. O conteúdo em `data/` segue as regras da seção acima.
