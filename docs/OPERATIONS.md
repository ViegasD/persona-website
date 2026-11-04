# Produção, créditos e atividade

No painel, abra **Pedidos → pedido → vídeo** para retomar uma falha ou gerar uma nova versão. Cada solicitação pode consumir créditos das APIs. O saldo de créditos do cliente não é debitado novamente pelo retrabalho da equipe.

As versões ficam privadas até a equipe assistir à prévia, conferir imagem, voz e conteúdo e clicar em **Publicar versão aprovada**. O vídeo anterior continua disponível até a publicação. **Retomar tarefa existente** consulta tarefas com identificador salvo; submissões sem confirmação ficam retidas para conferência no provedor. Descartar uma versão não estorna custos de API. A janela de processamento histórica continua sendo uma restrição do servidor.

**Créditos das APIs** permite configurar e-mail da equipe, limites de alerta e saldos informados manualmente. O destinatário inicial é `eduardolv100@gmail.com`. A consulta roda a cada cinco minutos e as falhas por falta de créditos também geram alertas. Há intervalo mínimo de 24 horas por provedor. O envio SMTP é marcado antes da tentativa: resultados ambíguos ficam retidos, evitando reenvios automáticos.

- KIE: saldo real em créditos KIE, quando a API estiver disponível.
- xAI: saldo pré-pago com `XAI_MANAGEMENT_API_KEY` e `XAI_TEAM_ID` no ambiente do servidor; a chave de inferência sozinha não consulta o saldo.
- OpenAI: a chave de geração não expõe saldo pré-pago. Um saldo informado permite estimar somente o consumo registrado neste aplicativo. Custos sem preços tornam a estimativa indisponível.

Os vídeos da conta registram reprodução iniciada após cerca de três segundos assistidos, chegada ao fim, clique em baixar e avaliação opcional de 1 a 5 com comentário. Esses sinais não comprovam atenção humana nem conclusão do download. São separados por versão. Prévias administrativas não contam. Não existe reconstrução retroativa desses eventos.

**Pixel e atividade** separa eventos do navegador, eventos do aplicativo e respostas da Meta Conversions API. Navegador enfileirado/biblioteca carregada não comprova entrega à Meta. CAPI aceita não comprova atribuição a anúncios. O funil usa pedidos criados no período e pagamentos confirmados no banco; inclui testes e eventuais reembolsos posteriores. Receita líquida fica no Financeiro. Os eventos locais duram 90 dias e não armazenam IP, parâmetros de URL ou conteúdo de formulários. A telemetria local respeita `Do Not Track`.

## Geração

`SCENE_IMAGE_PROVIDER=xai` é o padrão. `XAI_IMAGE_MODEL=grok-imagine-image-2.0` usa edição com as imagens de referência, 9:16, 1K, qualidade média e apenas uma saída. O custo real retornado pelo provedor é registrado; na ausência dele, o painel identifica uma estimativa. KIE permanece disponível com `SCENE_IMAGE_PROVIDER=kie`, sem fallback pago automático entre provedores.

O vídeo continua em `grok-imagine-video` com duração configurada de 15 segundos; não há migração automática para Video 1.5. O roteiro gerado procura 22–26 palavras em 15s; texto exato curto do cliente é preservado. A cena de aniversário pode incluir bolo e um pendão com até os dois primeiros nomes. A revisão exige identidade, fundo, ocasião e grafia legível antes do vídeo; falhas exigem intervenção, sem repetição paga automática. Gestos são pequenos, com câmera fixa e objetos apoiados. Uma imagem e um vídeo continuam sendo o fluxo normal.

Contratos e preços de referência: [edição multi-imagem xAI](https://docs.x.ai/developers/model-capabilities/images/multi-image-editing), [Grok Imagine Image 2.0](https://docs.x.ai/developers/models/grok-imagine-image-2.0), [saldo KIE](https://docs.kie.ai/common-api/get-account-credits).

## Anatomia e referências

Poses expressivas continuam permitidas. Use referências de catálogo com anatomia clara: cauda e patas distinguíveis, sem membros sobrepostos que pareçam duplicados. Troque a imagem de referência quando ela for ambígua; o prompt não é um substituto para essa correção.

A revisão de cenas exige anatomia correta. Antes da disponibilização automática, uma revisão independente observa seis quadros distribuídos pelo vídeo, buscando membros, orelhas ou caudas duplicados e alterações de identidade. `QUALITY_REVIEW_MODEL` escolhe o modelo dessa revisão (padrão `gpt-6.1-terra`); o consumo é registrado. Configure seu preço por tokens em Financeiro quando necessário. Vereditos ficam no armazenamento privado ao lado do vídeo (`.quality.json`).

Falhas e casos incertos ficam retidos para revisão; não provocam outra geração paga automaticamente. As prévias de tentativas com falha continuam disponíveis ao administrador. Esta revisão é probabilística e pode deixar passar defeitos entre quadros amostrados; não garante anatomia perfeita. As versões em rascunho também passam pela revisão antes de ficarem prontas para publicação.
