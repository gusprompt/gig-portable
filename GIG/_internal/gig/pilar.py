"""
Pilar: gera um mapa estrutural canonico a partir do processo completo e do recorte selecionado.

Entrada:
    - texto ancorado completo com [AIP: X]
    - in_sumario_completo.json
    - in_sumario_estrutural.json

Saida:
    - in_mapa_estrutural.json
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gig.structural_core import (
    prepare_processing_context,
    write_json_artifact,
)


SCHEMA_NAME = "StructuralMap.v1"
OUTPUT_FILENAME = "in_mapa_estrutural.json"
DEPURATION_LEVEL_NONE = "nenhum"
DEPURATION_LEVEL_CONSERVATIVE = "conservador"
DEPURATION_LEVEL_AGGRESSIVE = "agressivo"
_SPACED_UPPER_RE = re.compile(r"^(?:[A-ZÀ-Ý]\s*){4,}$")
_DOMAIN_LINE_RE = re.compile(
    r"^(?:www\.)?[a-z0-9][a-z0-9.\-]*\.(?:com|jus|gov|net|org)(?:\.br)?$",
    re.IGNORECASE,
)
_HEADER_HINTS = (
    "advogados",
    "advocacia",
    "direito de familia",
    "sucessoes",
    "contatos",
    "contato",
    "e mail",
    "email",
    "cep",
    "telefone",
    "jardim paulista",
    "sao paulo sp",
    "sao paulo/sp",
)
_FOOTER_HINTS = (
    "documento assinado digitalmente",
    "conforme impressao a margem direita",
    "conforme impressao a margem",
)
_BODY_ANCHOR_HINTS = (
    "excelentissimo",
    "excelentissima",
    "meritissimo",
    "meritissima",
    "processo",
    "acao ",
    "agravo",
    "embargos",
    "contestacao",
    "manifestacao",
    "alegacoes",
    "peticao",
    "requerente",
    "requerido",
    "vistos",
    "decisao",
    "despacho",
    "sentenca",
    "trata se",
)
_LEADING_FRAGMENT_DATE_RE = re.compile(r"^\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,4})?(?:\s|$)")


@dataclass
class PilarResult:
    structural_json_path: Path
    structural_dict: dict[str, Any]
    total_documentos: int
    total_paginas: int
    documentos_sem_texto: int


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z0-9 ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_line_for_match(line: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(line or "").strip())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9@:/+.\- ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _looks_spaced_upper(line: str) -> bool:
    compact = " ".join(str(line or "").strip().split())
    if not compact:
        return False
    return _SPACED_UPPER_RE.fullmatch(compact) is not None


def _is_single_letter_noise_line(line: str) -> bool:
    stripped = str(line or "").strip()
    return len(stripped) == 1 and stripped.isalpha()


def _is_body_anchor_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if any(normalized.startswith(hint) for hint in _BODY_ANCHOR_HINTS):
        return True
    word_count = len(normalized.split())
    if word_count >= 5 and len(normalized) >= 24 and not _is_header_noise_line(line):
        return True
    return False


def _is_substantial_body_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if _is_body_anchor_line(line):
        return True
    word_count = len(normalized.split())
    return word_count >= 7 and len(normalized) >= 42


def _looks_domain_contact_line(line: str) -> bool:
    compact = str(line or "").strip().lower()
    if not compact or " " in compact or "/" in compact:
        return False
    return _DOMAIN_LINE_RE.fullmatch(compact) is not None


def _is_repeated_top_noise_candidate(line: str) -> bool:
    return _is_header_noise_line(line) or _looks_domain_contact_line(line)


def _is_header_noise_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if _is_single_letter_noise_line(line):
        return True
    if _looks_spaced_upper(line):
        return True
    if any(hint in normalized for hint in _HEADER_HINTS):
        return True
    if any(token in normalized for token in ("av. ", "avenida ", "rua ", "alameda ", "cj.", "conj.", "+55 ")):
        return True
    if len(normalized) <= 2 and str(line or "").strip().isalnum():
        return True
    return False


def _is_footer_noise_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if any(hint in normalized for hint in _FOOTER_HINTS):
        return True
    return False


def _is_graphic_fragment_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if _is_body_anchor_line(line) or _is_header_noise_line(line) or _is_footer_noise_line(line):
        return False
    if normalized.startswith(("http://", "https://", "www.")):
        return False
    word_count = len(normalized.split())
    if _LEADING_FRAGMENT_DATE_RE.match(normalized) and len(normalized) <= 36:
        return True
    if word_count <= 4 and len(normalized) <= 28 and not normalized.endswith((".", ";", ":", "?", "!")):
        return True
    return False


def _collect_repeated_noise_lines(
    page_lines_map: dict[int, list[str]],
    *,
    depuration_level: str,
) -> tuple[set[str], set[str]]:
    top_counts: dict[str, int] = {}
    bottom_counts: dict[str, int] = {}

    for lines in page_lines_map.values():
        non_empty = [line for line in lines if line.strip()]
        top_slice = non_empty[:40]
        bottom_slice = non_empty[-10:]

        seen_top: set[str] = set()
        for line in top_slice:
            normalized = _normalize_line_for_match(line)
            if not normalized or normalized in seen_top:
                continue
            seen_top.add(normalized)
            if depuration_level == DEPURATION_LEVEL_CONSERVATIVE and not _is_repeated_top_noise_candidate(line):
                continue
            if len(normalized) <= 140:
                top_counts[normalized] = top_counts.get(normalized, 0) + 1

        seen_bottom: set[str] = set()
        for line in bottom_slice:
            normalized = _normalize_line_for_match(line)
            if not normalized or normalized in seen_bottom:
                continue
            seen_bottom.add(normalized)
            if depuration_level == DEPURATION_LEVEL_CONSERVATIVE and not _is_footer_noise_line(line):
                continue
            if len(normalized) <= 160:
                bottom_counts[normalized] = bottom_counts.get(normalized, 0) + 1

    repeated_top = {normalized for normalized, count in top_counts.items() if count >= 2}
    repeated_bottom = {normalized for normalized, count in bottom_counts.items() if count >= 2}
    return repeated_top, repeated_bottom


def _strip_header_block(lines: list[str], repeated_top: set[str], depuration_level: str) -> list[str]:
    if depuration_level == DEPURATION_LEVEL_NONE:
        return lines
    cut_idx = 0
    header_hits = 0
    header_started = False
    for idx, line in enumerate(lines[:64]):
        if not line.strip():
            if header_started:
                cut_idx = idx + 1
            continue
        normalized = _normalize_line_for_match(line)
        repeated_hit = normalized in repeated_top
        noise_hit = _is_header_noise_line(line)
        body_anchor_hit = _is_body_anchor_line(line)
        if depuration_level == DEPURATION_LEVEL_CONSERVATIVE:
            if repeated_hit or noise_hit:
                header_hits += 1
                header_started = True
                cut_idx = idx + 1
                continue
            if body_anchor_hit:
                break
            break
        if repeated_hit or noise_hit or len(normalized) <= 3:
            header_hits += 1
            header_started = True
            cut_idx = idx + 1
            continue
        if body_anchor_hit:
            break
        break
    if header_hits >= 2 and cut_idx > 0:
        return lines[cut_idx:]
    return lines


def _strip_footer_block(lines: list[str], repeated_bottom: set[str], depuration_level: str) -> list[str]:
    if depuration_level == DEPURATION_LEVEL_NONE:
        return lines
    cut_idx = len(lines)
    footer_hits = 0
    for idx in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        line = lines[idx]
        if not line.strip():
            cut_idx = idx
            continue
        normalized = _normalize_line_for_match(line)
        repeated_hit = normalized in repeated_bottom
        noise_hit = _is_footer_noise_line(line)
        if depuration_level == DEPURATION_LEVEL_CONSERVATIVE:
            if repeated_hit or noise_hit:
                footer_hits += 1
                cut_idx = idx
                continue
            break
        if repeated_hit or noise_hit or len(normalized) <= 3:
            footer_hits += 1
            cut_idx = idx
            continue
        break
    if footer_hits >= 1 and cut_idx < len(lines):
        return lines[:cut_idx]
    return lines


def _strip_leading_fragment_block(lines: list[str], depuration_level: str) -> list[str]:
    if depuration_level != DEPURATION_LEVEL_AGGRESSIVE:
        return lines
    cut_idx = 0
    fragment_hits = 0
    body_after_fragment = False
    fragment_started = False
    for idx, line in enumerate(lines[:28]):
        if not line.strip():
            if fragment_started:
                cut_idx = idx + 1
            continue
        if _is_graphic_fragment_line(line):
            fragment_hits += 1
            fragment_started = True
            cut_idx = idx + 1
            continue
        if fragment_started and _is_substantial_body_line(line):
            body_after_fragment = True
            break
        break
    if fragment_hits >= 5 and body_after_fragment and cut_idx > 0:
        return lines[cut_idx:]
    return lines


def _depurate_page_text(
    page_text: str,
    *,
    repeated_top: set[str],
    repeated_bottom: set[str],
    depuration_level: str,
) -> str:
    if depuration_level == DEPURATION_LEVEL_NONE:
        return page_text.strip()
    lines = page_text.splitlines()
    lines = _strip_header_block(lines, repeated_top, depuration_level)
    lines = _strip_footer_block(lines, repeated_bottom, depuration_level)
    lines = _strip_leading_fragment_block(lines, depuration_level)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_piece_text(
    page_dict: dict[int, str],
    pag_ini: int,
    pag_fim: int,
    *,
    depuration_level: str,
) -> str:
    raw_page_map: dict[int, str] = {}
    page_lines_map: dict[int, list[str]] = {}
    for page_number in range(pag_ini, pag_fim + 1):
        page_text = str(page_dict.get(page_number, "") or "").strip()
        raw_page_map[page_number] = page_text
        page_lines_map[page_number] = page_text.splitlines()

    repeated_top, repeated_bottom = _collect_repeated_noise_lines(
        page_lines_map,
        depuration_level=depuration_level,
    )

    parts: list[str] = []
    for page_number in range(pag_ini, pag_fim + 1):
        cleaned_page = _depurate_page_text(
            raw_page_map.get(page_number, ""),
            repeated_top=repeated_top,
            repeated_bottom=repeated_bottom,
            depuration_level=depuration_level,
        )
        marker = f"[AIP: {page_number}]"
        if cleaned_page:
            parts.append(f"{marker}\n{cleaned_page}")
        else:
            parts.append(marker)
    return "\n\n".join(parts).strip()


def _classify_document_class(categoria: str) -> str:
    normalized = _normalize_label(categoria)
    if not normalized:
        return "nao_classificado"
    if "ministerio publico" in normalized or "promotor" in normalized or "promotoria" in normalized:
        return "manifestacao_mp"
    if "sentenca" in normalized:
        return "sentenca_judicial"
    if "despacho" in normalized:
        return "despacho_judicial"
    if "decisao" in normalized:
        return "decisao_judicial"
    if any(
        token in normalized
        for token in (
            "peticao",
            "contestacao",
            "reconvencao",
            "replica",
            "impugnacao",
            "alegacoes",
            "recurso",
            "contrarrazoes",
            "manifestacao",
            "emenda",
            "inicial",
        )
    ):
        return "peticao_parte"
    if any(
        token in normalized
        for token in (
            "laudo",
            "pericia",
            "exame",
            "extrato",
            "comprovante",
            "foto",
            "fotografia",
            "audio",
            "video",
            "documentos diversos",
        )
    ):
        return "documento_probatorio"
    if any(
        token in normalized
        for token in (
            "procuracao",
            "substabelecimento",
            "certidao",
            "publicacao",
            "intimacao",
            "guia",
            "custas",
            "oficio",
            "mandado",
        )
    ):
        return "documento_auxiliar"
    return "nao_classificado"


def _requires_view_review(review_pages: set[int], pag_ini: int, pag_fim: int) -> bool:
    if not review_pages or pag_ini <= 0 or pag_fim <= 0 or pag_ini > pag_fim:
        return False
    for page_number in range(pag_ini, pag_fim + 1):
        if page_number in review_pages:
            return True
    return False


def build_structural_map(
    *,
    anchored_text: str,
    sumario_full: dict[str, Any],
    selected_summary: dict[str, Any] | None,
    output_dir: Path,
    source_pdf_name: str,
    source_summary_full_name: str,
    source_summary_selected_name: str,
    selection_mode: str,
    selection_origin: str,
    depuration_level: str,
    review_pages: set[int] | None = None,
    logger: Callable[[str], None] | None = None,
    schema_name: str = SCHEMA_NAME,
    output_filename: str = OUTPUT_FILENAME,
    tool_name: str = "Pilar",
    source_text_name: str = "",
) -> PilarResult:
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    context = prepare_processing_context(
        anchored_text=anchored_text,
        sumario=sumario_full,
        selected_summary=selected_summary,
    )

    log(
        "[Pilar] Base carregada: "
        f"{len(context.piece_refs)} item(ns) no sumario completo, "
        f"{len(context.selected_indexes)} selecao(oes) por indice, "
        f"{len(context.selected_files)} por arquivo, "
        f"include_all={context.include_all_items}."
    )

    review_lookup = set(review_pages or set())
    documentos: list[dict[str, Any]] = []
    documentos_sem_texto = 0

    for piece_ref in context.active_piece_refs:
        categoria = piece_ref.categoria
        pag_ini = piece_ref.pag_ini
        pag_fim = piece_ref.pag_fim
        if pag_ini > 0 and pag_fim > 0 and pag_ini <= pag_fim:
            texto = _extract_piece_text(
                context.page_dict,
                pag_ini,
                pag_fim,
                depuration_level=depuration_level,
            )
            aip_inicio = piece_ref.aip_inicio
            aip_fim = piece_ref.aip_fim
        else:
            texto = ""
            aip_inicio = ""
            aip_fim = ""

        has_text = bool(texto)
        if not has_text:
            documentos_sem_texto += 1

        documentos.append(
            {
                "ordem_compilacao": piece_ref.ordem_compilacao,
                "categoria_filtro": categoria,
                "classe_documental": _classify_document_class(categoria),
                "arquivo_original": piece_ref.arquivo_original,
                "aip_inicio": aip_inicio,
                "aip_fim": aip_fim,
                "texto": texto,
                "has_text": has_text,
                "requires_view_review": _requires_view_review(review_lookup, pag_ini, pag_fim),
            }
        )

    estrutura = {
        "schema": str(schema_name or "").strip() or SCHEMA_NAME,
        "versao": "1.0",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "origem_ferramenta": str(tool_name or "").strip() or "Pilar",
        "selection_mode": str(selection_mode or "").strip() or "categorias",
        "selection_origin": str(selection_origin or "").strip() or "manual",
        "depuration_level": str(depuration_level or "").strip() or DEPURATION_LEVEL_CONSERVATIVE,
        "source_summary_full": source_summary_full_name,
        "source_summary_selected": source_summary_selected_name,
        "source_pdf": source_pdf_name,
        "total_paginas_pdf": context.total_pages,
        "total_documentos_selecionados": len(documentos),
        "documentos_sem_texto": documentos_sem_texto,
        "documentos": documentos,
    }
    if str(source_text_name or "").strip():
        estrutura["source_text"] = str(source_text_name).strip()

    output_path = write_json_artifact(
        output_dir=output_dir,
        filename=str(output_filename or "").strip() or OUTPUT_FILENAME,
        payload=estrutura,
    )
    log(
        "[Pilar] Salvo: "
        f"{output_path} (documentos={len(documentos)}, sem_texto={documentos_sem_texto})"
    )

    return PilarResult(
        structural_json_path=output_path,
        structural_dict=estrutura,
        total_documentos=len(documentos),
        total_paginas=context.total_pages,
        documentos_sem_texto=documentos_sem_texto,
    )
