# Estrutura Modular de Agentes (GIG)

Este diretorio centraliza a versao modular dos agentes.
Padrao adotado:

- `agents/<agente>/`
- Cada agente contem:
 - `manifest.json`
 - `metadata.json`
 - `boot.json`
 - `schemas.json`
 - `extras.json` (campos adicionais do agente)
 - `modules/*.json` (um arquivo por modulo)
- `dist/agent.<agente>.json` (saida monolitica para consumo do app)

## Estado atual do app

- Agente core em uso operacional: `monk-lite`
- Os agentes removidos do app ativo foram arquivados em `03_LEGACY`

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

## Terminologia obrigatoria

- `dependencias_saida`: usar apenas nomes com prefixo `out_`
- `saida_visual`: usar apenas nomes com prefixo `view_`
- Instrucoes de modulo devem referenciar `[OUT:<nome>]` no contrato tecnico
- Entradas canonicas devem usar prefixo `in_`
- Arquivos de resposta bruta devem usar prefixo `raw_`
- Metadados de modulo devem usar prefixo `meta_`
- Consolidados finais devem usar prefixo `final_`
- Artefatos de execucao devem usar prefixo `run_`
