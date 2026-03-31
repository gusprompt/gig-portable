# Estrutura Modular de Speks (GIG)

Este diretorio centraliza a versao modular dos speks.

Nota terminologica:

- `spek` e a nomenclatura atual da classe de produto
- `agents/` permanece como diretorio legado durante a transicao
- nomes como `agent.<agente>.json` ainda existem por compatibilidade estrutural

Padrao adotado:

- `agents/<agente>/`
- Cada spek contem:
 - `manifest.json`
 - `metadata.json`
 - `boot.json`
 - `schemas.json`
  - `extras.json` (campos adicionais do spek)
  - `modules/*.json` (um arquivo por modulo)
- `dist/agent.<agente>.json` (saida monolitica para consumo do app)

## Estado atual do app

- Spek core em uso operacional: `monk-lite`
- Os speks removidos do app ativo foram arquivados em `03_LEGACY`

## Comandos

No workspace `G:\Meu Drive\1_IA\SEE-Py`:

```powershell
# modularizar todos os agent.*.json da raiz GIG_agents
python .\AGENTS\GIG_agents\scripts\agent_modular_manager.py split-all `
 --agents-dir .\AGENTS\GIG_agents\dist\agents `
 --modular-root .\AGENTS\GIG_agents\modular

# compilar todos os manifests para dist/
python .\AGENTS\GIG_agents\scripts\agent_modular_manager.py build-all `
 --modular-root .\AGENTS\GIG_agents\modular
```

## Observacao

O app continua lendo JSON monolitico normalmente.
A estrutura modular serve como fonte de manutencao e auditoria.

Referencias do repo:

- `docs/apresentacao/GIG.md`
- `docs/operacional/GIG_V2_TEMPLATE_PACOTE_AGENTE.md`
- `docs/operacional/FERRAMENTAS_E_ARTEFATOS_PARA_AGENTES.md`

## Terminologia obrigatoria

- `dependencias_saida`: usar apenas nomes com prefixo `out_`
- `saida_visual`: usar apenas nomes com prefixo `view_`
- Instrucoes de modulo devem referenciar `[OUT:<nome>]` no contrato tecnico
- Entradas canonicas devem usar prefixo `in_`
- Arquivos de resposta bruta devem usar prefixo `raw_`
- Metadados de modulo devem usar prefixo `meta_`
- Consolidados finais devem usar prefixo `final_`
- Artefatos de execucao devem usar prefixo `run_`
