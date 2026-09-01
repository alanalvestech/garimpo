# radar

Notícias diárias de tecnologia, em português, reunidas de agregadores públicos do GitHub.

Todo dia de manhã uma rotina lê o que as fontes publicaram, traduz e resume. Em [`data/`](data) cada pasta é um tipo de conteúdo (Arxiv, HackerNews, ProductHunt, Twitter e por aí), e guarda o último dia que aquela fonte publicou, em Markdown para ler e em JSON para consultar. Os dias anteriores ficam no histórico do git.

## Assinar

O [`rss.xml`](rss.xml) na raiz junta todas as categorias, e cada pasta em `data/` tem o feed dela. Para assinar, cole no seu leitor a URL do arquivo cru:

```
https://raw.githubusercontent.com/alanalvestech/radar/main/rss.xml
https://raw.githubusercontent.com/alanalvestech/radar/main/data/Arxiv/arxiv.xml
```

O feed guarda os últimos 50 itens, então quem abre o leitor uma vez por semana não perde o que já saiu de `data/`.

## Fontes e direitos

Cada item traz o link para a publicação original. Os agregadores de onde o radar lê a lista não aparecem nos arquivos: eles são o caminho, não a fonte.

Resumo traduzido só entra quando a licença do agregador permite republicar. Quando não permite, o item fica com título e link, que é o mínimo para achar a matéria.

Os direitos das matérias são de seus veículos. Este repositório reúne referências, links e resumos, e não substitui a leitura na fonte.

## Licença

O código está sob MIT. O conteúdo em `data/` segue as regras da seção acima.
