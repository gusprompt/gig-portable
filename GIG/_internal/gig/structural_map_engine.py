"""
Motor compartilhado de mapa estrutural.

Este modulo concentra a implementacao neutra usada por Filtro e variantes
canonicas para produzir artefatos estruturais a partir de texto ancorado e
sumarios do processo.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gig.structural_core import (
    prepare_processing_context,
    write_json_artifact,
)


SCHEMA_NAME = "StructuralMap.v2"
OUTPUT_FILENAME = "in_mapa_estrutural.json"
DEPURATION_LEVEL_NONE = "nenhum"
DEPURATION_LEVEL_CONSERVATIVE = "conservador"
DEPURATION_LEVEL_AGGRESSIVE = "agressivo"
_SPACED_UPPER_RE = re.compile(r"^(?:[A-ZÀ-Ý]\s*){4,}$")
_DOMAIN_LINE_RE = re.compile(
    r"^(?:www\.)?[a-z0-9][a-z0-9.\-]*\.(?:com|jus|gov|net|org)(?:\.br)?$",
    re.IGNORECASE,
)
_EMAIL_LINE_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", re.IGNORECASE)
_GRAPHIC_SEPARATOR_RE = re.compile(r"^(?:[_=\-~.]{6,}|[_=\-~.\s]{8,})$")
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
_PROCESS_METADATA_HINTS = (
    "processo digital",
    "processo n",
    "classe assunto",
    "classe  assunto",
    "classe - assunto",
    "requerente",
    "requerido",
    "autor da heranca",
    "autor da heranca passivo",
    "autor da heranca (passivo)",
    "juiz de direito",
    "juiza de direito",
    "foro regional",
    "certidao processo",
    "certidao de remessa de relacao",
    "certidao de publicacao de relacao",
    "emitido em",
    "teor do ato",
    "pagina ",
    "pagina:",
    "tramitação prioritaria",
    "tramitacao prioritaria",
    "justica gratuita",
)
_BODY_SECTION_HINTS = (
    "da justica gratuita",
    "dos fatos",
    "do direito",
    "dos pedidos",
    "termos em que",
    "pede deferimento",
    "relatorio",
    "fundamentacao",
    "dispositivo",
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
    "agravo",
    "embargos",
    "contestacao",
    "manifestacao",
    "alegacoes",
    "peticao",
    "vistos",
    "decisao",
    "despacho",
    "sentenca",
    "trata se",
)
_LEADING_FRAGMENT_DATE_RE = re.compile(r"^\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,4})?(?:\s|$)")


@dataclass
class StructuralMapResult:
    structural_json_path: Path
    structural_raw_json_path: Path | None
    audit_report_path: Path | None
    refinement_report_path: Path | None
    structural_dict: dict[str, Any]
    total_documentos: int
    total_paginas: int
    documentos_sem_texto: int


# Alias de compatibilidade: varios chamadores ainda conhecem o resultado
# estrutural pelo nome legado ligado ao Pilar.
PilarResult = StructuralMapResult


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-z0-9 ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _build_omit_category_lookup(
    omit_transcription_categories: set[str] | frozenset[str] | None,
) -> set[str]:
    normalized: set[str] = set()
    for category in omit_transcription_categories or set():
        key = _normalize_label(str(category or ""))
        if key:
            normalized.add(key)
    return normalized


def _normalize_line_for_match(line: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(line or "").strip())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9@:/+.\- ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _contains_normalized_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_text = str(normalized_text or "").strip()
    normalized_phrase = _normalize_line_for_match(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    pattern = r"(?:^| )" + r"\s+".join(re.escape(part) for part in normalized_phrase.split()) + r"(?: |$)"
    return re.search(pattern, normalized_text) is not None


def _starts_with_normalized_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_text = str(normalized_text or "").strip()
    normalized_phrase = _normalize_line_for_match(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    return normalized_text == normalized_phrase or normalized_text.startswith(normalized_phrase + " ")


def _looks_spaced_upper(line: str) -> bool:
    compact = " ".join(str(line or "").strip().split())
    if not compact:
        return False
    if _SPACED_UPPER_RE.fullmatch(compact) is None:
        return False
    tokens = compact.split()
    if len(tokens) < 4:
        return False
    return all(len(token) == 1 and token.isalpha() for token in tokens)


def _is_single_letter_noise_line(line: str) -> bool:
    stripped = str(line or "").strip()
    return len(stripped) == 1 and stripped.isalpha()


def _is_body_anchor_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if any(normalized.startswith(hint) for hint in _BODY_ANCHOR_HINTS):
        return True
    if any(normalized.startswith(hint) for hint in _BODY_SECTION_HINTS):
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


def _looks_email_contact_line(line: str) -> bool:
    compact = str(line or "").strip().lower()
    if not compact or " " in compact:
        return False
    return _EMAIL_LINE_RE.fullmatch(compact) is not None


def _looks_phone_contact_line(line: str) -> bool:
    compact = str(line or "").strip()
    if not compact:
        return False
    if any(ch.isalpha() for ch in compact):
        return False
    digits = "".join(ch for ch in compact if ch.isdigit())
    return 10 <= len(digits) <= 13 and len(digits) >= max(1, len(compact) // 3)


def _is_graphic_separator_line(line: str) -> bool:
    compact = " ".join(str(line or "").strip().split())
    if not compact:
        return False
    return _GRAPHIC_SEPARATOR_RE.fullmatch(compact) is not None


def _is_repeated_top_noise_candidate(line: str) -> bool:
    return (
        _is_graphic_separator_line(line)
        or _is_header_noise_line(line)
        or _looks_domain_contact_line(line)
        or _looks_process_metadata_line(line)
        or _looks_professional_identity_line(line)
    )


def _looks_process_metadata_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if any(_starts_with_normalized_phrase(normalized, hint) for hint in _PROCESS_METADATA_HINTS):
        return True
    if ":" in normalized and any(_contains_normalized_phrase(normalized, hint) for hint in _PROCESS_METADATA_HINTS):
        return True
    return False


def _looks_professional_identity_line(line: str) -> bool:
    normalized = _normalize_line_for_match(line)
    compact = " ".join(str(line or "").strip().split())
    if not normalized or not compact:
        return False
    if any(token in normalized for token in ("oab", "advogad", "estagiari", "defensoria", "procurador", "procuradora")):
        return True
    if len(compact) > 80 or any(ch.isdigit() for ch in compact):
        return False
    tokens = [token for token in re.split(r"\s+", compact) if token]
    if len(tokens) < 2 or len(tokens) > 6:
        return False
    upper_like = sum(
        1
        for token in tokens
        if token.replace(".", "").replace("-", "").isupper()
    )
    if upper_like < max(2, len(tokens) - 1):
        return False
    normalized_compact = _normalize_line_for_match(compact)
    if any(normalized_compact.startswith(hint) for hint in _BODY_SECTION_HINTS):
        return False
    return True


def _is_header_noise_line(line: str) -> bool:
    if _is_graphic_separator_line(line):
        return True
    normalized = _normalize_line_for_match(line)
    if not normalized:
        return False
    if _is_single_letter_noise_line(line):
        return True
    if _looks_spaced_upper(line):
        return True
    if _looks_email_contact_line(line) or _looks_phone_contact_line(line):
        return True
    if normalized.startswith(("contato:", "contatos:", "telefone:", "telefones:", "e-mails:", "emails:")):
        return True
    if any(_contains_normalized_phrase(normalized, hint) for hint in _HEADER_HINTS):
        return True
    if _looks_process_metadata_line(line):
        return True
    if any(token in normalized for token in ("oab", "advogad", "estagiari", "tel.", "tel ", "email:", "e mail:")):
        return True
    if any(
        _starts_with_normalized_phrase(normalized, token)
        for token in ("av.", "avenida", "rua", "alameda", "cj.", "conj.", "+55")
    ):
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
            if body_anchor_hit and not repeated_hit and not noise_hit:
                break
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
) -> list[dict[str, Any]]:
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

    parts: list[dict[str, Any]] = []
    for page_number in range(pag_ini, pag_fim + 1):
        cleaned_page = _depurate_page_text(
            raw_page_map.get(page_number, ""),
            repeated_top=repeated_top,
            repeated_bottom=repeated_bottom,
            depuration_level=depuration_level,
        )
        parts.append(
            {
                "aip": page_number,
                "conteudo": cleaned_page,
            }
        )
    return parts


def _build_raw_output_filename(output_filename: str) -> str:
    path = Path(str(output_filename or "").strip() or OUTPUT_FILENAME)
    if path.suffix.lower() == ".json":
        return f"{path.stem}.raw.json"
    return f"{path.name}.raw.json"


def _build_audit_report_filename(output_filename: str) -> str:
    path = Path(str(output_filename or "").strip() or OUTPUT_FILENAME)
    if path.suffix.lower() == ".json":
        return f"{path.stem}.audit_report.json"
    return f"{path.name}.audit_report.json"


def _preview_text(value: str, *, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _build_document_record(
    *,
    ordem_compilacao: int,
    categoria: str,
    classe_documental: str,
    arquivo_original: str,
    aip_inicio: str,
    aip_fim: str,
    texto: list[dict[str, Any]],
    has_text: bool,
    requires_view_review: bool,
    texto_omitido: bool = False,
    texto_omitido_motivo: str = "",
) -> dict[str, Any]:
    return {
        "aip_inicio": aip_inicio,
        "aip_fim": aip_fim,
        "ordem_compilacao": ordem_compilacao,
        "categoria_filtro": categoria,
        "classe_documental": classe_documental,
        "arquivo_original": arquivo_original,
        "texto": texto,
        "has_text": has_text,
        "texto_omitido": texto_omitido,
        "texto_omitido_motivo": texto_omitido_motivo,
        "requires_view_review": requires_view_review,
    }


def _normalize_lines_for_diff(text: str) -> list[str]:
    return [
        _normalize_line_for_match(line)
        for line in str(text or "").splitlines()
        if _normalize_line_for_match(line)
    ]


def _collect_removed_lines(raw_text: str, refined_text: str) -> list[str]:
    raw_lines = [str(line or "") for line in str(raw_text or "").splitlines()]
    refined_lines = [str(line or "") for line in str(refined_text or "").splitlines()]
    raw_counter = Counter(
        normalized
        for normalized in (_normalize_line_for_match(line) for line in raw_lines)
        if normalized
    )
    refined_counter = Counter(
        normalized
        for normalized in (_normalize_line_for_match(line) for line in refined_lines)
        if normalized
    )
    removed_budget = {
        normalized: raw_counter.get(normalized, 0) - refined_counter.get(normalized, 0)
        for normalized in raw_counter
        if raw_counter.get(normalized, 0) - refined_counter.get(normalized, 0) > 0
    }

    removed_lines: list[str] = []
    for raw_line in raw_lines:
        normalized = _normalize_line_for_match(raw_line)
        if not normalized:
            continue
        remaining = removed_budget.get(normalized, 0)
        if remaining <= 0:
            continue
        removed_lines.append(raw_line)
        removed_budget[normalized] = remaining - 1
    return removed_lines


def _infer_page_change_types(removed_lines: list[str]) -> list[str]:
    normalized_removed_lines = [
        _normalize_line_for_match(line)
        for line in removed_lines
        if _normalize_line_for_match(line)
    ]

    change_types: set[str] = set()
    for line in normalized_removed_lines:
        if not line:
            continue
        if _is_graphic_separator_line(line):
            change_types.add("removed_graphic_separator")
        if any(hint in line for hint in _FOOTER_HINTS):
            change_types.add("removed_digital_signature_footer")
        if _is_header_noise_line(line) or _looks_process_metadata_line(line):
            change_types.add("removed_repeated_header")
        if (
            line.startswith(("contato:", "contatos:", "telefone:", "telefones:", "e-mails:", "emails:"))
            or "@" in line
        ):
            change_types.add("removed_repeated_contact_footer")

    if not change_types:
        change_types.add("page_content_changed")
    return sorted(change_types)


def _render_removed_text(removed_lines: list[str]) -> str:
    filtered_lines = [str(line or "") for line in removed_lines]
    if not filtered_lines:
        return ""
    return "\n".join(filtered_lines).strip()


def _build_audit_report(
    *,
    raw_payload: dict[str, Any],
    refined_payload: dict[str, Any],
    source_raw_filename: str,
    target_filename: str,
    source_pdf_name: str,
    source_text_name: str,
    selection_mode: str,
    selection_origin: str,
    depuration_level: str,
    conversion_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_documents = list(raw_payload.get("documentos") or [])
    refined_documents = list(refined_payload.get("documentos") or [])
    removals: list[dict[str, Any]] = list((conversion_metadata or {}).get("cleanup_removals") or [])
    pages_changed = 0
    removed_chars_total = sum(int(item.get("removed_char_count") or 0) for item in removals)

    for raw_doc, refined_doc in zip(raw_documents, refined_documents):
        raw_text_rows = list(raw_doc.get("texto") or [])
        refined_text_rows = list(refined_doc.get("texto") or [])

        for raw_page, refined_page in zip(raw_text_rows, refined_text_rows):
            aip = raw_page.get("aip")
            raw_text = str(raw_page.get("conteudo") or "")
            refined_text = str(refined_page.get("conteudo") or "")
            if raw_text == refined_text:
                continue
            removed_lines = _collect_removed_lines(raw_text, refined_text)
            removed_text = _render_removed_text(removed_lines)
            if not removed_text:
                continue
            pages_changed += 1
            removed_chars_total += len(removed_text)
            removals.append(
                {
                    "stage": "refinement",
                    "rule": _infer_page_change_types(removed_lines),
                    "aip": aip,
                    "ordem_compilacao": raw_doc.get("ordem_compilacao"),
                    "arquivo_original": raw_doc.get("arquivo_original"),
                    "removed_text": removed_text,
                    "removed_char_count": len(removed_text),
                }
            )

    pages_with_removals = sorted(
        {
            int(item.get("aip"))
            for item in removals
            if isinstance(item.get("aip"), int)
        }
    )
    stage_summary: dict[str, dict[str, int]] = {}
    for item in removals:
        stage = str(item.get("stage") or "").strip() or "unknown"
        bucket = stage_summary.setdefault(
            stage,
            {"entries": 0, "pages": 0, "removed_chars_total": 0},
        )
        bucket["entries"] += 1
        bucket["removed_chars_total"] += int(item.get("removed_char_count") or 0)
    for stage in stage_summary:
        stage_pages = {
            int(item.get("aip"))
            for item in removals
            if str(item.get("stage") or "").strip() == stage and isinstance(item.get("aip"), int)
        }
        stage_summary[stage]["pages"] = len(stage_pages)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_version": "v2-minimal",
        "source_pdf": source_pdf_name,
        "source_text": source_text_name,
        "source_raw_json": source_raw_filename,
        "target_json": target_filename,
        "summary": {
            "removal_entries": len(removals),
            "pages_with_removals": len(pages_with_removals),
            "removed_chars_total": removed_chars_total,
            "by_stage": stage_summary,
        },
        "removals": removals,
    }


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
    manifest_pages: list[dict[str, Any]] | None = None,
    conversion_metadata: dict[str, Any] | None = None,
    ocr_runtime_config: dict[str, Any] | None = None,
    omit_transcription_categories: set[str] | frozenset[str] | None = None,
) -> StructuralMapResult:
    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    tool_label = str(tool_name or "").strip() or "Pilar"

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    context = prepare_processing_context(
        anchored_text=anchored_text,
        sumario=sumario_full,
        selected_summary=selected_summary,
    )
    omit_categories_lookup = _build_omit_category_lookup(omit_transcription_categories)
    total_paginas_pdf = len(manifest_pages) if isinstance(manifest_pages, list) and manifest_pages else context.total_pages

    log(
        f"[{tool_label}] Base carregada: "
        f"{len(context.piece_refs)} item(ns) no sumario completo, "
        f"{len(context.selected_indexes)} selecao(oes) por indice, "
        f"{len(context.selected_files)} por arquivo, "
        f"include_all={context.include_all_items}."
    )
    if omit_categories_lookup:
        log(
            f"[{tool_label}] Categorias com transcricao omitida: "
            + ", ".join(sorted(omit_categories_lookup))
        )

    review_lookup = set(review_pages or set())
    documentos: list[dict[str, Any]] = []
    raw_documentos: list[dict[str, Any]] = []
    documentos_sem_texto = 0

    for piece_ref in context.active_piece_refs:
        categoria = piece_ref.categoria
        pag_ini = piece_ref.pag_ini
        pag_fim = piece_ref.pag_fim
        if pag_ini > 0 and pag_fim > 0 and pag_ini <= pag_fim:
            raw_texto = _extract_piece_text(
                context.page_dict,
                pag_ini,
                pag_fim,
                depuration_level=DEPURATION_LEVEL_NONE,
            )
            texto = _extract_piece_text(
                context.page_dict,
                pag_ini,
                pag_fim,
                depuration_level=depuration_level,
            )
            aip_inicio = piece_ref.aip_inicio
            aip_fim = piece_ref.aip_fim
        else:
            raw_texto = []
            texto = []
            aip_inicio = ""
            aip_fim = ""

        has_text = any(str(page.get("conteudo") or "").strip() for page in texto)
        if not has_text:
            documentos_sem_texto += 1

        categoria_key = _normalize_label(categoria)
        texto_omitido = categoria_key in omit_categories_lookup
        texto_omitido_motivo = "categoria_excluida" if texto_omitido else ""
        classe_documental = _classify_document_class(categoria)
        requires_view_review = _requires_view_review(review_lookup, pag_ini, pag_fim)
        documentos.append(
            _build_document_record(
                ordem_compilacao=piece_ref.ordem_compilacao,
                categoria=categoria,
                classe_documental=classe_documental,
                arquivo_original=piece_ref.arquivo_original,
                aip_inicio=aip_inicio,
                aip_fim=aip_fim,
                texto=[] if texto_omitido else texto,
                has_text=has_text,
                requires_view_review=requires_view_review,
                texto_omitido=texto_omitido,
                texto_omitido_motivo=texto_omitido_motivo,
            )
        )
        raw_documentos.append(
            _build_document_record(
                ordem_compilacao=piece_ref.ordem_compilacao,
                categoria=categoria,
                classe_documental=classe_documental,
                arquivo_original=piece_ref.arquivo_original,
                aip_inicio=aip_inicio,
                aip_fim=aip_fim,
                texto=[] if texto_omitido else raw_texto,
                has_text=any(str(page.get("conteudo") or "").strip() for page in raw_texto),
                requires_view_review=requires_view_review,
                texto_omitido=texto_omitido,
                texto_omitido_motivo=texto_omitido_motivo,
            )
        )

    raw_estrutura = {
        "schema": str(schema_name or "").strip() or SCHEMA_NAME,
        "versao": "2.0",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "origem_ferramenta": str(tool_name or "").strip() or "Pilar",
        "selection_mode": str(selection_mode or "").strip() or "categorias",
        "selection_origin": str(selection_origin or "").strip() or "manual",
        "depuration_level": DEPURATION_LEVEL_NONE,
        "refinement_stage": "raw",
        "target_depuration_level": str(depuration_level or "").strip() or DEPURATION_LEVEL_CONSERVATIVE,
        "source_summary_full": source_summary_full_name,
        "source_summary_selected": source_summary_selected_name,
        "source_pdf": source_pdf_name,
        "total_paginas_pdf": total_paginas_pdf,
        "total_documentos_selecionados": len(raw_documentos),
        "documentos_sem_texto": documentos_sem_texto,
        "documentos": raw_documentos,
    }
    if str(source_text_name or "").strip():
        raw_estrutura["source_text"] = str(source_text_name).strip()

    estrutura = copy.deepcopy(raw_estrutura)
    estrutura["depuration_level"] = str(depuration_level or "").strip() or DEPURATION_LEVEL_CONSERVATIVE
    estrutura["refinement_stage"] = "refined"
    estrutura["documentos"] = documentos

    for stale_path in (
        output_dir / _build_raw_output_filename(output_filename),
        output_dir / _build_audit_report_filename(output_filename),
    ):
        if not stale_path.exists():
            continue
        try:
            stale_path.unlink()
        except Exception:
            pass

    output_path = write_json_artifact(
        output_dir=output_dir,
        filename=str(output_filename or "").strip() or OUTPUT_FILENAME,
        payload=estrutura,
    )
    log(
        f"[{tool_label}] Salvo: "
        f"{output_path} (documentos={len(documentos)}, sem_texto={documentos_sem_texto})"
    )
    return StructuralMapResult(
        structural_json_path=output_path,
        structural_raw_json_path=None,
        audit_report_path=None,
        refinement_report_path=None,
        structural_dict=estrutura,
        total_documentos=len(documentos),
        total_paginas=context.total_pages,
        documentos_sem_texto=documentos_sem_texto,
    )
