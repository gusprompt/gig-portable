"""
Dissector: extrai e pre-processa texto por peca processual a partir do texto ancorado.

Entrada: processo_texto_ancorado.txt + sumario_pecas_completo.json
Saida:   in_context_map.json (zero chamadas de IA)

Modo standalone:
    python gig/dissector.py <pdf_path> <sumario_json_path> <output_dir>
    python gig/dissector.py <pdf_path> <sumario_json_path> <output_dir> --anchored-text <path>
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gig.structural_core import (
    build_piece_base_fields,
    prepare_processing_context,
    to_int as _to_int,
    write_json_artifact,
)

# Palavras-chave que identificam pecas processuais (categoria ja explica a peca)
# Pecas com essas palavras-chave recebem apenas um preview de 400 chars.
# Pecas sem essas palavras-chave (ex: "Documentos Diversos") recebem 3000 chars para
# que o MONK possa LER o conteudo e classificar corretamente.
_PROCESSUAL_KEYWORDS: frozenset[str] = frozenset([
    "decisao", "despacho", "peticao", "contestacao", "sentenca", "ata",
    "audiencia", "certidao", "intimacao", "publicacao", "decurso", "guia",
    "custas", "mandado", "oficio", "procuracao", "manifestacao", "laudo",
    "parecer", "alegacoes", "emenda", "tutela", "juntada", "reconvencao",
    "impugnacao", "replica", "inicial", "ministerio",
])

TEXT_LIMIT_PROCESSUAL = 400    # Preview: categoria ja identifica a peca
TEXT_LIMIT_PROBATORIO = 3_000  # Texto completo: MONK precisa LER para classificar

SCHEMA_NAME = "DissectorEstrutura.v1"
OUTPUT_FILENAME = "in_context_map.json"
GROUPED_OUTPUT_FILENAME = "in_context_map_grouped.json"
LACUNAS_OUTPUT_FILENAME = "in_context_gaps_map.json"
OVERVIEW_OUTPUT_FILENAME = "in_context_overview.json"
DEFAULT_SELECTED_SUMMARY_SOURCE = "in_sumario_estrutural.json"
DEFAULT_OMIT_TEXT_CATEGORIES: frozenset[str] = frozenset({"Certidão de Publicação"})


@dataclass
class DissectionResult:
    """Resultado da dissecacao do processo."""

    estrutura_json_path: Path
    grouped_json_path: Path
    lacunas_json_path: Path
    overview_json_path: Path
    estrutura_dict: dict
    total_pecas: int
    total_paginas: int
    pecas_sem_texto: int


def _normalize_category_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z0-9 ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _build_omit_category_lookup(omit_text_categories: set[str] | frozenset[str] | None) -> set[str]:
    source = omit_text_categories if omit_text_categories is not None else _resolve_default_omit_text_categories()
    normalized: set[str] = set()
    for category in source:
        key = _normalize_category_label(str(category or ""))
        if key:
            normalized.add(key)
    return normalized


def _read_env_positive_int(key: str, default: int) -> int:
    raw = str(os.getenv(key, "") or "").strip()
    if not raw:
        return int(default)
    try:
        parsed = int(raw)
    except Exception:
        return int(default)
    if parsed <= 0:
        return int(default)
    return int(parsed)


def _resolve_text_limits() -> tuple[int, int]:
    processual = _read_env_positive_int("GIG_DISSECTOR_LIMIT_PROCESSUAL", TEXT_LIMIT_PROCESSUAL)
    probatorio = _read_env_positive_int("GIG_DISSECTOR_LIMIT_PROBATORIO", TEXT_LIMIT_PROBATORIO)
    return processual, probatorio


def _resolve_default_omit_text_categories() -> set[str]:
    raw = str(os.getenv("GIG_DISSECTOR_OMIT_CATEGORIES", "") or "").strip()
    if not raw:
        return set(DEFAULT_OMIT_TEXT_CATEGORIES)
    items: list[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        category = chunk.strip()
        if category:
            items.append(category)
    if not items:
        return set(DEFAULT_OMIT_TEXT_CATEGORIES)
    return set(items)


def _classify_peca_profile(categoria_filtro: str) -> str:
    """
    Determina o perfil da peca para definir o limite de texto extraido.

    Returns:
        "processual" — categoria especifica; 400 chars suficientes.
        "probatorio" — categoria vaga (Documentos Diversos, etc.); 3000 chars necessarios.
    """
    # Remove acentos antes da limpeza para preservar letras em categorias PT-BR
    # (ex.: "Petição" -> "peticao", "Ofício" -> "oficio").
    normalized = unicodedata.normalize("NFKD", (categoria_filtro or "").lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z]", "", normalized)
    for keyword in _PROCESSUAL_KEYWORDS:
        if keyword in normalized:
            return "processual"
    return "probatorio"


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """
    Trunca texto ao limite especificado.

    Returns:
        (texto, foi_truncado)
    """
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def _extract_peca_text(
    page_dict: dict[int, str],
    pag_ini: int,
    pag_fim: int,
    total_pages: int,
) -> tuple[str, str, str]:
    """
    Extrai e concatena o texto de um intervalo de paginas.

    Returns:
        (texto_concatenado, ancora_inicio, ancora_fim)
    """
    _ = total_pages
    ancora_inicio = f"[AIP: {pag_ini}]"
    ancora_fim = f"[AIP: {pag_fim}]"
    parts: list[str] = []
    for page_num in range(pag_ini, pag_fim + 1):
        page_text = page_dict.get(page_num, "")
        if page_text:
            parts.append(page_text)
    return "\n".join(parts).strip(), ancora_inicio, ancora_fim


def _build_context_fields(
    *,
    piece_ref: Any,
) -> dict[str, Any]:
    return build_piece_base_fields(piece_ref)


def _normalize_file_group_key(arquivo_original: str) -> str:
    nome = str(arquivo_original or "").strip()
    if not nome:
        return "(sem_arquivo)"
    nome = re.sub(
        r"\s*\(pag\.?\s*\d+\s*[-–]\s*\d+\)\s*(?=(\.[^.]+)?$)",
        "",
        nome,
        flags=re.IGNORECASE,
    )
    nome = re.sub(
        r"\s*\(pag\.?\s*\d+\)\s*(?=(\.[^.]+)?$)",
        "",
        nome,
        flags=re.IGNORECASE,
    )
    nome = re.sub(r"\s{2,}", " ", nome).strip()
    return nome or "(sem_arquivo)"


def _group_items_by_file(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        arquivo_original = str(row.get("arquivo_original", "") or "").strip()
        arquivo_base = _normalize_file_group_key(arquivo_original)
        grouped.setdefault(arquivo_base, []).append(dict(row))

    arquivos: dict[str, dict[str, Any]] = {}
    for arquivo_base, itens in grouped.items():
        itens_ordenados = sorted(
            itens,
            key=lambda item: (
                _to_int(item.get("ordem_compilacao")) or 0,
                str(item.get("aip_inicio") or ""),
            ),
        )
        arquivos[arquivo_base] = {
            "total_itens": len(itens),
            "itens": itens_ordenados,
        }
    return arquivos


def _group_items_by_category(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        categoria = str(row.get("categoria", "") or "").strip() or "Sem classificacao"
        grouped.setdefault(categoria, []).append(dict(row))

    categorias: dict[str, dict[str, Any]] = {}
    for categoria in sorted(grouped.keys(), key=lambda item: item.lower()):
        itens = grouped[categoria]
        arquivos = _group_items_by_file(itens)
        categorias[categoria] = {
            "total_itens": len(itens),
            "total_arquivos_base": len(arquivos),
            "arquivos": arquivos,
        }
    return categorias


def _build_overview_bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
    arquivos = _group_items_by_file(items)
    return {
        "total_itens": len(items),
        "total_arquivos_base": len(arquivos),
        "arquivos": arquivos,
    }


def _build_overview_sections(
    pecas: list[dict[str, Any]],
    lacunas_sem_texto: list[dict[str, Any]],
) -> dict[str, Any]:
    pecas_selecionadas = [row for row in pecas if isinstance(row, dict) and bool(row.get("fornecida_integralmente"))]
    arquivos_excluidos = [row for row in pecas if isinstance(row, dict) and bool(row.get("texto_omitido_por_categoria"))]
    arquivos_com_texto = [
        row for row in pecas
        if isinstance(row, dict) and bool(row.get("has_text")) and not bool(row.get("texto_omitido"))
    ]
    arquivos_gap = [row for row in lacunas_sem_texto if isinstance(row, dict)]
    return {
        "pecas_selecionadas": _build_overview_bucket(pecas_selecionadas),
        "arquivos_excluidos": _build_overview_bucket(arquivos_excluidos),
        "arquivos_com_texto": _build_overview_bucket(arquivos_com_texto),
        "arquivos_gap": _build_overview_bucket(arquivos_gap),
    }


def _build_grouped_context_map_payload(
    estrutura: dict[str, Any],
    lacunas_sem_texto: list[dict[str, Any]],
) -> dict[str, Any]:
    _ = lacunas_sem_texto
    pecas = estrutura.get("pecas", [])
    if not isinstance(pecas, list):
        pecas = []
    arquivos = _group_items_by_file(pecas)
    categorias = _group_items_by_category(pecas)
    return {
        "schema": "ContextMapGrouped.v4",
        "versao": "4.0",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "origem": OUTPUT_FILENAME,
        "total_arquivos_base": len(arquivos),
        "total_categorias": len(categorias),
        "total_pecas": estrutura.get("total_pecas", len(pecas)),
        "total_paginas_pdf": estrutura.get("total_paginas_pdf", 0),
        "pecas_sem_texto": estrutura.get("pecas_sem_texto", 0),
        "arquivos": arquivos,
        "categorias": categorias,
    }


def _build_overview_context_map_payload(
    estrutura: dict[str, Any],
    lacunas_sem_texto: list[dict[str, Any]],
) -> dict[str, Any]:
    pecas = estrutura.get("pecas", [])
    if not isinstance(pecas, list):
        pecas = []
    overview = _build_overview_sections(pecas, lacunas_sem_texto)
    return {
        "schema": "ContextOverview.v1",
        "versao": "1.0",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "origem": OUTPUT_FILENAME,
        "origem_grouped": GROUPED_OUTPUT_FILENAME,
        "total_pecas": estrutura.get("total_pecas", len(pecas)),
        "total_paginas_pdf": estrutura.get("total_paginas_pdf", 0),
        "pecas_sem_texto": estrutura.get("pecas_sem_texto", 0),
        "pecas_selecionadas": overview["pecas_selecionadas"],
        "arquivos_excluidos": overview["arquivos_excluidos"],
        "arquivos_com_texto": overview["arquivos_com_texto"],
        "arquivos_gap": overview["arquivos_gap"],
    }


def _build_gaps_tree_by_classification(
    lacunas_sem_texto: list[dict[str, Any]],
) -> dict[str, Any]:
    def group_gap_items_by_file(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in items:
            if not isinstance(row, dict):
                continue
            arquivo_original = str(row.get("arquivo_original", "") or "").strip()
            arquivo_base = _normalize_file_group_key(arquivo_original)
            grouped.setdefault(arquivo_base, []).append(dict(row))

        arquivos: dict[str, dict[str, Any]] = {}
        for arquivo_base, rows in grouped.items():
            ocorrencias_ordenadas = sorted(
                rows,
                key=lambda item: (
                    _to_int(item.get("ordem_compilacao")) or 0,
                    str(item.get("aip_inicio") or ""),
                ),
            )
            arquivos[arquivo_base] = {
                "total_ocorrencias": len(rows),
                "ocorrencias": ocorrencias_ordenadas,
            }
        return arquivos

    grouped_by_class: dict[str, list[dict[str, Any]]] = {}
    for row in lacunas_sem_texto:
        if not isinstance(row, dict):
            continue
        categoria = str(row.get("categoria", "") or "").strip() or "Sem classificacao"
        grouped_by_class.setdefault(categoria, []).append(row)

    classificacoes: dict[str, Any] = {}
    for categoria in sorted(grouped_by_class.keys(), key=lambda item: item.lower()):
        items = grouped_by_class[categoria]
        arquivos = group_gap_items_by_file(items)
        classificacoes[categoria] = {
            "total_ocorrencias": len(items),
            "total_arquivos_base": len(arquivos),
            "arquivos": arquivos,
        }

    return {
        "total_classificacoes": len(classificacoes),
        "classificacoes": classificacoes,
    }


def dissect_process(
    anchored_text: str,
    sumario: dict,
    output_dir: Path,
    selected_summary: dict | None = None,
    selected_summary_source: str = DEFAULT_SELECTED_SUMMARY_SOURCE,
    omit_text_categories: set[str] | frozenset[str] | None = None,
    logger: Callable[[str], None] | None = None,
) -> DissectionResult:
    """
    Disseca o processo: extrai texto por peca e salva in_context_map.json.

    Args:
        anchored_text: Texto completo com marcadores [AIP: X]
        sumario: Dict do sumario_pecas_completo.json (com campo "itens")
        output_dir: Pasta de saida (onde in_context_map.json sera salvo)
        logger: Callback(msg: str) para log de progresso

    Returns:
        DissectionResult com caminho e estatisticas
    """
    def log(msg: str) -> None:
        if logger:
            logger(msg)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    context = prepare_processing_context(
        anchored_text=anchored_text,
        sumario=sumario,
        selected_summary=selected_summary,
    )
    log(
        f"[DISSECTOR] Texto ancorado: {context.total_pages} paginas, "
        f"{len(context.page_dict)} com conteudo."
    )

    total_input = len(context.piece_refs)
    log(f"[DISSECTOR] Sumario: {total_input} pecas a processar.")
    omit_categories_lookup = _build_omit_category_lookup(omit_text_categories)
    text_limit_processual, text_limit_probatorio = _resolve_text_limits()
    if context.selected_indexes or context.selected_files:
        log(
            "[DISSECTOR] Pecas com fornecimento integral identificado: "
            f"{len(context.selected_indexes)} por indice, {len(context.selected_files)} por nome."
        )
    if omit_categories_lookup:
        log(
            "[DISSECTOR] Categorias com transcricao omitida: "
            + ", ".join(sorted(omit_categories_lookup))
        )
    log(
        "[DISSECTOR] Limites de texto: "
        f"processual={text_limit_processual}, probatorio={text_limit_probatorio}"
    )
    pecas: list[dict] = []
    lacunas_sem_texto: list[dict[str, Any]] = []
    pecas_sem_texto = 0
    pecas_omitidas_por_categoria = 0

    for piece_ref in context.piece_refs:
        ordem = piece_ref.ordem_compilacao
        categoria = piece_ref.categoria
        arquivo = piece_ref.arquivo_original
        categoria_key = _normalize_category_label(categoria)
        texto_omitido_por_categoria = categoria_key in omit_categories_lookup
        fornecida_integralmente = (
            (ordem, arquivo) in context.active_piece_keys and not context.include_all_items
        )
        if texto_omitido_por_categoria:
            pecas_omitidas_por_categoria += 1

        pag_ini = piece_ref.pag_ini
        pag_fim = piece_ref.pag_fim
        context_fields = _build_context_fields(
            piece_ref=piece_ref,
        )

        if pag_ini <= 0 or pag_fim <= 0 or pag_ini > pag_fim:
            pecas_sem_texto += 1
            texto_omitido = (fornecida_integralmente or texto_omitido_por_categoria)
            pecas.append({
                **context_fields,
                "texto": "",
                "texto_truncado": False,
                "has_text": False,
                "texto_omitido": texto_omitido,
                "fornecida_integralmente": fornecida_integralmente,
                "texto_omitido_por_categoria": texto_omitido_por_categoria,
                "texto_omitido_motivo": (
                    "fornecida_integralmente"
                    if fornecida_integralmente
                    else ("categoria_excluida" if texto_omitido_por_categoria else "")
                ),
                "fonte_fornecimento": (
                    selected_summary_source if fornecida_integralmente else ""
                ),
            })
            if not fornecida_integralmente and not texto_omitido_por_categoria and not texto_omitido:
                lacunas_sem_texto.append(
                    {
                        **context_fields,
                        "motivo": "intervalo_paginas_invalido",
                    }
                )
            continue

        texto_raw, _, _ = _extract_peca_text(
            context.page_dict, pag_ini, pag_fim, context.total_pages
        )

        has_text = bool(texto_raw)
        if not has_text:
            pecas_sem_texto += 1

        texto_omitido = (fornecida_integralmente or texto_omitido_por_categoria)
        if texto_omitido:
            texto = ""
            truncado = False
        else:
            profile = _classify_peca_profile(categoria)
            limit = text_limit_processual if profile == "processual" else text_limit_probatorio
            texto, truncado = _truncate_text(texto_raw, limit)

        pecas.append({
            **context_fields,
            "texto": texto,
            "texto_truncado": truncado,
            "has_text": has_text,
            "texto_omitido": texto_omitido,
            "fornecida_integralmente": fornecida_integralmente,
            "texto_omitido_por_categoria": texto_omitido_por_categoria,
            "texto_omitido_motivo": (
                "fornecida_integralmente"
                if fornecida_integralmente
                else ("categoria_excluida" if texto_omitido_por_categoria else "")
            ),
            "fonte_fornecimento": (
                selected_summary_source if fornecida_integralmente else ""
            ),
        })
        if not fornecida_integralmente and not texto_omitido_por_categoria and not texto_omitido and not has_text:
            lacunas_sem_texto.append(
                {
                    **context_fields,
                    "motivo": "sem_texto_extraido",
                }
            )

    log(
        f"[DISSECTOR] {total_input} pecas dissecadas. "
        f"Sem texto: {pecas_sem_texto}. "
        f"Omitidas por categoria: {pecas_omitidas_por_categoria}."
    )

    estrutura = {
        "schema": SCHEMA_NAME,
        "versao": "1.0",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "total_pecas": total_input,
        "total_paginas_pdf": context.total_pages,
        "pecas_sem_texto": pecas_sem_texto,
        "pecas": pecas,
    }

    output_path = write_json_artifact(
        output_dir=output_dir,
        filename=OUTPUT_FILENAME,
        payload=estrutura,
    )
    log(f"[DISSECTOR] Salvo: {output_path}")

    grouped_payload = _build_grouped_context_map_payload(estrutura, lacunas_sem_texto)
    grouped_output_path = write_json_artifact(
        output_dir=output_dir,
        filename=GROUPED_OUTPUT_FILENAME,
        payload=grouped_payload,
    )
    log(f"[DISSECTOR] Salvo: {grouped_output_path}")

    lacunas_payload = {
        "schema": "ContextGapsMap.v3",
        "versao": "3.0",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "origem": OUTPUT_FILENAME,
        "criterios_aplicados": {
            "fornecida_integralmente": False,
            "texto_omitido_por_categoria": False,
            "texto_omitido": False,
            "has_text": False,
        },
        "total_pecas": total_input,
        "total_lacunas_sem_texto": len(lacunas_sem_texto),
        "arvore_por_classificacao": _build_gaps_tree_by_classification(lacunas_sem_texto),
    }
    lacunas_output_path = write_json_artifact(
        output_dir=output_dir,
        filename=LACUNAS_OUTPUT_FILENAME,
        payload=lacunas_payload,
    )
    log(
        f"[DISSECTOR] Salvo: {lacunas_output_path} "
        f"(lacunas sem texto: {len(lacunas_sem_texto)})"
    )

    overview_payload = _build_overview_context_map_payload(estrutura, lacunas_sem_texto)
    overview_output_path = write_json_artifact(
        output_dir=output_dir,
        filename=OVERVIEW_OUTPUT_FILENAME,
        payload=overview_payload,
    )
    log(f"[DISSECTOR] Salvo: {overview_output_path}")

    return DissectionResult(
        estrutura_json_path=output_path,
        grouped_json_path=grouped_output_path,
        lacunas_json_path=lacunas_output_path,
        overview_json_path=overview_output_path,
        estrutura_dict=estrutura,
        total_pecas=total_input,
        total_paginas=context.total_pages,
        pecas_sem_texto=pecas_sem_texto,
    )


# ======================================================================
# MODO STANDALONE (CLI)
# ======================================================================

def _extract_text_from_pdf_standalone(pdf_path: Path) -> str:
    """
    Extrai texto do PDF com marcadores [AIP: X] usando PyMuPDF.
    Versao simplificada para uso standalone (sem OCR, sem dependencias do GIG).
    """
    try:
        import fitz  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            "PyMuPDF nao instalado. Execute: pip install pymupdf"
        )
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    parts: list[str] = []
    for i in range(total_pages):
        page_num = i + 1
        text = (doc[i].get_text("text") or "").strip()
        _ = total_pages
        parts.append(f"[AIP: {page_num}]\n{text}")
    doc.close()
    return "\n\n".join(parts)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="DISSECTOR: extrai estrutura de texto por peca processual.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python gig/dissector.py processo.pdf sumario.json ./saida\n"
            "  python gig/dissector.py processo.pdf sumario.json ./saida "
            "--anchored-text texto.txt\n"
        ),
    )
    parser.add_argument("pdf_path", help="Caminho do processo_completo.pdf")
    parser.add_argument("sumario_json_path", help="Caminho do sumario_pecas_completo.json")
    parser.add_argument("output_dir", help="Pasta de saida para in_context_map.json")
    parser.add_argument(
        "--anchored-text",
        metavar="PATH",
        default=None,
        help="Caminho do processo_texto_ancorado.txt (se ja gerado)",
    )
    parser.add_argument(
        "--selected-summary",
        metavar="PATH",
        default=None,
        help="Caminho do in_sumario_estrutural.json para marcar pecas fornecidas integralmente",
    )
    parser.add_argument(
        "--omit-category",
        metavar="NOME",
        action="append",
        default=None,
        help="Categoria para omitir transcricao de texto (pode repetir)",
    )
    args = parser.parse_args()

    pdf_path_arg = Path(args.pdf_path)
    sumario_path_arg = Path(args.sumario_json_path)
    output_dir_arg = Path(args.output_dir)

    if not pdf_path_arg.exists():
        print(f"[ERRO] PDF nao encontrado: {pdf_path_arg}", file=sys.stderr)
        sys.exit(1)
    if not sumario_path_arg.exists():
        print(f"[ERRO] Sumario nao encontrado: {sumario_path_arg}", file=sys.stderr)
        sys.exit(1)

    # Localiza ou gera o texto ancorado
    if args.anchored_text:
        anchored_text_path_arg = Path(args.anchored_text)
        if not anchored_text_path_arg.exists():
            print(
                f"[ERRO] Texto ancorado nao encontrado: {anchored_text_path_arg}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"[DISSECTOR] Usando texto ancorado: {anchored_text_path_arg}")
        anchored_text_content = anchored_text_path_arg.read_text(encoding="utf-8")
    else:
        candidate_text = output_dir_arg / "processo_texto_ancorado.txt"
        if candidate_text.exists():
            print(f"[DISSECTOR] Reutilizando texto ancorado: {candidate_text}")
            anchored_text_content = candidate_text.read_text(encoding="utf-8")
        else:
            print(f"[DISSECTOR] Gerando texto ancorado a partir de: {pdf_path_arg}")
            anchored_text_content = _extract_text_from_pdf_standalone(pdf_path_arg)
            output_dir_arg.mkdir(parents=True, exist_ok=True)
            saved_path = output_dir_arg / "processo_texto_ancorado.txt"
            saved_path.write_text(anchored_text_content, encoding="utf-8")
            print(f"[DISSECTOR] Texto ancorado salvo: {saved_path}")

    sumario_content = json.loads(sumario_path_arg.read_text(encoding="utf-8"))
    selected_summary_content: dict | None = None
    if args.selected_summary:
        selected_summary_path_arg = Path(args.selected_summary)
        if not selected_summary_path_arg.exists():
            print(
                f"[ERRO] Sumario selecionado nao encontrado: {selected_summary_path_arg}",
                file=sys.stderr,
            )
            sys.exit(1)
        selected_summary_content = json.loads(
            selected_summary_path_arg.read_text(encoding="utf-8")
        )

    result = dissect_process(
        anchored_text=anchored_text_content,
        sumario=sumario_content,
        output_dir=output_dir_arg,
        selected_summary=selected_summary_content,
        omit_text_categories=(set(args.omit_category) if args.omit_category else None),
        logger=print,
    )

    print(f"\n[DISSECTOR] Concluido: {result.total_pecas} pecas, {result.total_paginas} paginas.")
    print(f"[DISSECTOR] Pecas sem texto: {result.pecas_sem_texto}")
    print(f"[DISSECTOR] Arquivo gerado: {result.estrutura_json_path}")
    print(f"[DISSECTOR] Mapa agrupado: {result.grouped_json_path}")
    print(f"[DISSECTOR] Lacunas sem texto: {result.lacunas_json_path}")
    print(f"[DISSECTOR] Visao geral: {result.overview_json_path}")
