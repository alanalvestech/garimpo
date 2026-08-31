# radar

Notícias diárias de tecnologia, em português, reunidas de agregadores públicos do GitHub.

Todo dia às 07:00 (horário de Brasília) uma rotina lê os arquivos mais recentes das fontes listadas em [`config/sources.yaml`](config/sources.yaml), traduz e resume com o Gemini, e grava dois arquivos em [`data/`](data):

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

Cada fonte declara em `config/sources.yaml` o modo de uso, e o modo segue a licença do repositório de origem:

- **full**: o conteúdo é traduzido e republicado aqui, com atribuição e link para o original. Usado apenas quando a licença permite (hoje, MIT).
- **link**: o repositório de origem não declara licença, então aqui fica apenas o ponteiro para o arquivo original.

Os direitos das matérias originais são de seus veículos. Este repositório reúne referências, links e resumos, e não substitui a leitura na fonte.

## Licença

O código deste repositório está sob MIT. O conteúdo em `data/` segue as regras da seção acima.
