from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_ai_page_utils import extract_page_range_from_summary_row


_AI_PAGE_RE = re.compile(
    r"(?:\[AIP:\s*(?P<aip_page>\d+)\s*\]|\[\[AI_PAGE:\s*(?P<legacy_page>\d+)\s*/\s*(?P<legacy_total>\d+)\s*\]\])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StructuralPieceRef:
    ordem_compilacao: int
    categoria: str
    arquivo_original: str
    pag_ini: int
    pag_fim: int
    aip_inicio: str
    aip_fim: str
    item_raw: dict[str, Any]


@dataclass(frozen=True)
class StructuralProcessingContext:
    page_dict: dict[int, str]
    total_pages: int
    piece_refs: list[StructuralPieceRef]
    selected_indexes: set[int]
    selected_files: set[str]
    include_all_items: bool
    active_piece_refs: list[StructuralPieceRef]
    active_piece_keys: set[tuple[int, str]]


def to_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def normalize_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", normalized)


def format_aip_marker(page_number: int) -> str:
    if isinstance(page_number, int) and page_number > 0:
        return f"[AIP: {page_number}]"
    return ""


def parse_anchored_text_to_pages(anchored_text: str) -> tuple[dict[int, str], int]:
    total_pages = 0
    page_dict: dict[int, str] = {}
    markers = list(_AI_PAGE_RE.finditer(anchored_text))
    if not markers:
        return page_dict, total_pages

    for idx, marker in enumerate(markers):
        page_num = int(marker.group("aip_page") or marker.group("legacy_page") or "0")
        legacy_total = marker.group("legacy_total")
        total_pages = max(total_pages, int(legacy_total) if legacy_total else page_num)
        start = marker.end()
        end = markers[idx + 1].start() if idx + 1 < len(markers) else len(anchored_text)
        text = anchored_text[start:end].strip()
        if page_num not in page_dict:
            page_dict[page_num] = text
    return page_dict, total_pages


def build_selected_lookup(selected_summary: dict[str, Any] | None) -> tuple[set[int], set[str]]:
    if not isinstance(selected_summary, dict):
        return set(), set()
    items = selected_summary.get("itens", [])
    if not isinstance(items, list):
        return set(), set()

    selected_indexes: set[int] = set()
    selected_files: set[str] = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status_fornecimento") or "").strip().lower()
        if status and status != "selecionada":
            continue
        idx = to_int(row.get("ordem_compilacao"))
        if idx is None:
            idx = to_int(row.get("indice"))
        if isinstance(idx, int) and idx > 0:
            selected_indexes.add(idx)
        file_name = normalize_token(str(row.get("arquivo_original") or ""))
        if file_name:
            selected_files.add(file_name)
    return selected_indexes, selected_files


def is_selected_piece(
    *,
    item_index: int,
    arquivo_original: str,
    selected_indexes: set[int],
    selected_files: set[str],
) -> bool:
    if item_index > 0 and item_index in selected_indexes:
        return True
    normalized_file = normalize_token(arquivo_original)
    if normalized_file and normalized_file in selected_files:
        return True
    return False


def build_piece_refs(sumario: dict[str, Any] | None) -> list[StructuralPieceRef]:
    if not isinstance(sumario, dict):
        return []
    items = sumario.get("itens", [])
    if not isinstance(items, list):
        return []

    refs: list[StructuralPieceRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ordem = to_int(item.get("ordem_compilacao"))
        if ordem is None:
            ordem = to_int(item.get("indice"))
        categoria = str(item.get("categoria") or item.get("categoria_filtro") or "").strip()
        arquivo_original = str(item.get("arquivo_original") or "").strip()
        pag_ini_raw, pag_fim_raw = extract_page_range_from_summary_row(item)
        pag_ini = pag_ini_raw or 0
        pag_fim = pag_fim_raw or 0
        refs.append(
            StructuralPieceRef(
                ordem_compilacao=ordem if isinstance(ordem, int) else 0,
                categoria=categoria,
                arquivo_original=arquivo_original,
                pag_ini=pag_ini,
                pag_fim=pag_fim,
                aip_inicio=format_aip_marker(pag_ini),
                aip_fim=format_aip_marker(pag_fim),
                item_raw=item,
            )
        )
    return refs


def prepare_processing_context(
    *,
    anchored_text: str,
    sumario: dict[str, Any] | None,
    selected_summary: dict[str, Any] | None,
) -> StructuralProcessingContext:
    page_dict, total_pages = parse_anchored_text_to_pages(anchored_text)
    piece_refs = build_piece_refs(sumario)
    selected_indexes, selected_files = build_selected_lookup(selected_summary)
    include_all_items = selected_summary is None
    active_piece_refs = (
        list(piece_refs)
        if include_all_items
        else [
            piece_ref
            for piece_ref in piece_refs
            if is_selected_piece(
                item_index=piece_ref.ordem_compilacao,
                arquivo_original=piece_ref.arquivo_original,
                selected_indexes=selected_indexes,
                selected_files=selected_files,
            )
        ]
    )
    active_piece_keys = {
        (piece_ref.ordem_compilacao, piece_ref.arquivo_original)
        for piece_ref in active_piece_refs
    }
    return StructuralProcessingContext(
        page_dict=page_dict,
        total_pages=total_pages,
        piece_refs=piece_refs,
        selected_indexes=selected_indexes,
        selected_files=selected_files,
        include_all_items=include_all_items,
        active_piece_refs=active_piece_refs,
        active_piece_keys=active_piece_keys,
    )


def iter_active_piece_refs(
    context: StructuralProcessingContext,
) -> list[StructuralPieceRef]:
    return list(context.active_piece_refs)


def build_piece_base_fields(piece_ref: StructuralPieceRef) -> dict[str, Any]:
    return {
        "ordem_compilacao": piece_ref.ordem_compilacao,
        "arquivo_original": piece_ref.arquivo_original,
        "categoria": piece_ref.categoria,
        "aip_inicio": piece_ref.aip_inicio,
        "aip_fim": piece_ref.aip_fim,
    }


def write_json_artifact(
    *,
    output_dir: Path,
    filename: str,
    payload: dict[str, Any],
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / str(filename or "").strip()
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path
