import argparse
import os
import json
import queue
import re
import shutil
import tempfile
import threading
import traceback
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

from artifact_naming import (
    FILTER_BATCH_FULL_SUMMARY_JSON_NAME,
    FILTER_FULL_PROCESS_PDF_NAME,
    FILTER_FULL_SUMMARY_JSON_NAME,
    FILTER_SELECTED_PDF_GLOB,
    build_filter_lot_name,
    build_filter_lot_pdf_name,
    build_filter_lot_summary_json_name,
    build_filter_selected_pdf_name,
    build_filter_selected_summary_json_name,
    extract_filter_selected_execution_number,
    msg_filter_full_pdf_generated,
    msg_filter_full_pdf_not_generated,
    msg_filter_generating_full_pdf,
)


PAGE_SUFFIX_RE = re.compile(r"\s*\((?:p(?:a|\u00e1)g(?:ina)?|pag)\s+\d+(?:\s*-\s*\d+)?\)\s*$", re.IGNORECASE)
PAGE_RANGE_RE = re.compile(r"\((?:p(?:a|\u00e1)g(?:ina)?|pag)\s+(\d+)(?:\s*-\s*(\d+))?\)\s*$", re.IGNORECASE)
FOLIO_SELECTION_TOKEN_RE = re.compile(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*$")
INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*]+')
RULES_FILE = Path(__file__).with_name("classification_rules.json")
DEFAULT_OUTPUT_ENV_VAR = "FILTRO_OUTPUT_DIR"
DEFAULT_BATCH_MAX_SIZE_MB = 25
BATCH_CUT_MODE_MB = "mb"
BATCH_CUT_MODE_TOKENS = "tokens"
DEFAULT_BATCH_CUT_MODE = BATCH_CUT_MODE_MB
DEFAULT_BATCH_TARGET_TOKENS = 90_000
DEFAULT_BATCH_MIN_TOKENS = 80_000
DEFAULT_BATCH_MAX_TOKENS = 100_000
ESTIMATED_TOKENS_PER_FOLIO = 700
DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS = 23.0
FILTER_FULL_SUMMARY_RUNTIME_JSON_NAME = "_in_sumario_completo_runtime.json"
SIGNATURE_STRIP_PRESETS_PT: tuple[float, ...] = (23.0, 28.0, 32.0, 36.0)
PHOTO_MARKER_KIND_WITH_TEXT = "fotografia_com_texto"
PHOTO_MARKER_KIND_PURE = "fotografia_pura"
PHOTO_HINT_TERMS = (
    "foto",
    "fotografia",
    "imagem",
    "img",
    "selfie",
)
PHOTO_WITH_TEXT_STRONG_HINT_TERMS = (
    "whatsapp",
    "chat",
    "conversa",
    "print",
    "screenshot",
    "captura",
)
PHOTO_WITH_TEXT_HINT_TERMS = (
    "whatsapp",
    "chat",
    "conversa",
    "print",
    "screenshot",
    "captura",
    "documento",
    "doc",
    "rg",
    "cpf",
    "cnh",
    "comprovante",
    "boleto",
    "receita",
    "atestado",
)

DEFAULT_RULES: list[dict[str, Any]] = [
    {"id": "ministerio_publico", "label": "Manifestacao/Parecer MP", "all_terms": ["ministerio publico"], "any_terms": ["manifestacao", "parecer"]},
    {"id": "decisao", "label": "Decisao", "all_terms": [], "any_terms": ["decisao"]},
    {"id": "despacho", "label": "Despacho", "all_terms": [], "any_terms": ["despacho"]},
    {"id": "peticao", "label": "Peticao", "all_terms": [], "any_terms": ["peticao"]},
    {"id": "contestacao", "label": "Contestacao", "all_terms": [], "any_terms": ["contestacao"]},
    {"id": "laudo_pericial", "label": "Laudo pericial", "all_terms": ["laudo"], "any_terms": ["pericia", "pericial"]},
]


@dataclass
class ProcessingConfig:
    zip_path: Path
    output_dir: Path
    main_rules: list[dict[str, Any]]
    main_categories_override: set[str] | None = None
    main_file_indexes_override: set[int] | None = None
    main_folio_ranges_override: list[tuple[int, int]] | None = None
    integral_mode: bool = False
    batch_only_mode: bool = False
    batch_max_size_mb: int = DEFAULT_BATCH_MAX_SIZE_MB
    batch_cut_mode: str = DEFAULT_BATCH_CUT_MODE
    batch_target_tokens: int = DEFAULT_BATCH_TARGET_TOKENS
    batch_min_tokens: int = DEFAULT_BATCH_MIN_TOKENS
    batch_max_tokens: int = DEFAULT_BATCH_MAX_TOKENS
    replace_photo_pages: bool = True
    crop_signature_strip_right: bool = False
    crop_signature_strip_width_points: float = DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS


@dataclass
class ProcessingResult:
    output_root: Path
    processing_mode: str
    integral_mode: bool
    total_files: int
    main_files: int
    auxiliary_files: int
    ia_pdf_path: Path | None
    ia_pdf_inputs: int
    ia_pdf_skipped: int
    ia_pdf_total_pages: int
    selection_criteria: str
    selected_summary_json_path: Path | None
    selected_summary_md_path: Path | None
    full_summary_json_path: Path
    batch_output_root: Path | None
    batch_full_summary_json_path: Path | None
    batch_total: int


@dataclass
class PiecePdfInput:
    order_key: tuple[int, int, int, int]
    source_index: int
    original_name: str
    category: str
    pdf_path: Path
    folio_start: int | None
    folio_end: int | None


def _contains_any_term(haystack: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        if term in haystack:
            return True
    return False


def detect_photo_marker_kind(piece: PiecePdfInput) -> str | None:
    normalized_context = normalize_for_match(f"{piece.category} {Path(piece.original_name).stem}")
    if not normalized_context:
        return None

    if _contains_any_term(normalized_context, PHOTO_WITH_TEXT_STRONG_HINT_TERMS):
        return PHOTO_MARKER_KIND_WITH_TEXT

    if not _contains_any_term(normalized_context, PHOTO_HINT_TERMS):
        return None

    if _contains_any_term(normalized_context, PHOTO_WITH_TEXT_HINT_TERMS):
        return PHOTO_MARKER_KIND_WITH_TEXT
    return PHOTO_MARKER_KIND_PURE


def sanitize_component(name: str, fallback: str = "SemCategoria") -> str:
    cleaned = INVALID_CHARS_RE.sub("_", name).strip().rstrip(".")
    return cleaned or fallback


def default_output_dir() -> Path:
    configured_global = os.environ.get("GIG_DEFAULT_OUTPUT_DIR", "").strip()
    if configured_global:
        return Path(configured_global).expanduser()
    configured = os.environ.get(DEFAULT_OUTPUT_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()

    home = Path.home()
    downloads = home / "Downloads"
    base_dir = downloads if downloads.exists() else home
    return base_dir / "FILTRO" / "saida"


def parse_output_dir(value: str | None) -> Path:
    raw = (value or "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_output_dir()


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    counter = 2
    stem = path.stem
    suffix = path.suffix
    parent = path.parent

    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def base_category_from_name(filename: str) -> str:
    stem = Path(filename).stem.strip()
    stem = PAGE_SUFFIX_RE.sub("", stem).strip()
    return sanitize_component(stem)


def normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    no_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", no_accents).strip().lower()


def _normalize_term_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Campo '{field_name}' deve ser lista de textos.")

    terms: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"Campo '{field_name}' deve conter apenas textos.")
        term = item.strip()
        if term:
            terms.append(term)
    return terms


def validate_rules(raw_rules: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_rules, list):
        raise ValueError("As regras devem ser uma lista.")
    if not raw_rules:
        raise ValueError("A lista de regras nao pode ser vazia.")

    validated: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Regra {index} precisa ser objeto JSON.")

        rule_id = str(raw.get("id", "")).strip()
        label = str(raw.get("label", "")).strip()

        if not rule_id:
            raise ValueError(f"Regra {index} sem campo 'id'.")
        if rule_id in used_ids:
            raise ValueError(f"ID duplicado nas regras: '{rule_id}'.")
        if not label:
            raise ValueError(f"Regra '{rule_id}' sem campo 'label'.")

        all_terms = _normalize_term_list(raw.get("all_terms"), "all_terms")
        any_terms = _normalize_term_list(raw.get("any_terms"), "any_terms")
        if not all_terms and not any_terms:
            raise ValueError(f"Regra '{rule_id}' precisa ter ao menos um termo em all_terms ou any_terms.")

        used_ids.add(rule_id)
        validated.append(
            {
                "id": rule_id,
                "label": label,
                "all_terms": all_terms,
                "any_terms": any_terms,
            }
        )

    return validated


def parse_rules_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if "rules" not in payload:
            raise ValueError("JSON invalido. Use uma lista de regras ou objeto com chave 'rules'.")
        payload = payload["rules"]
    return validate_rules(payload)


def save_rules(path: Path, rules: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_rules(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        rules = validate_rules(DEFAULT_RULES)
        save_rules(path, rules)
        return rules

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Arquivo de regras invalido: {exc}") from exc

    return parse_rules_payload(payload)


def matches_rule(normalized_text: str, rule: dict[str, Any]) -> bool:
    all_terms = [normalize_for_match(term) for term in rule["all_terms"]]
    any_terms = [normalize_for_match(term) for term in rule["any_terms"]]

    all_ok = all(term in normalized_text for term in all_terms) if all_terms else True
    any_ok = any(term in normalized_text for term in any_terms) if any_terms else True
    return all_ok and any_ok


def classify_master_folder(category: str, rules: list[dict[str, Any]]) -> tuple[str, str]:
    normalized = normalize_for_match(category)

    for rule in rules:
        if matches_rule(normalized, rule):
            return "pecas_principais", str(rule["id"])

    return "documentos_auxiliares", "outros"


def collect_zip_categories(zip_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        for entry in zf.infolist():
            if entry.is_dir():
                continue
            category = base_category_from_name(entry.filename)
            counts[category] = counts.get(category, 0) + 1
    return counts


def collect_zip_piece_candidates(zip_path: Path, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = [entry for entry in zf.infolist() if not entry.is_dir()]
        for idx, entry in enumerate(entries, start=1):
            original_name = entry.filename
            category = base_category_from_name(original_name)
            folio_start, folio_end = extract_folio_range(original_name)
            suggested = classify_master_folder(category, rules)[0] == "pecas_principais"
            pieces.append(
                {
                    "source_index": idx,
                    "arquivo_original": original_name,
                    "categoria": category,
                    "fls_inicial_original": folio_start,
                    "fls_final_original": folio_end,
                    "sugerida": suggested,
                }
            )

    pieces.sort(key=lambda item: page_sort_key(str(item["arquivo_original"]), int(item["source_index"])))
    return pieces


def extract_folio_range(filename: str) -> tuple[int | None, int | None]:
    stem = Path(filename).stem.strip()
    match = PAGE_RANGE_RE.search(stem)
    if not match:
        return None, None

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        return None, None
    return start, end


def page_sort_key(filename: str, fallback_index: int) -> tuple[int, int, int, int]:
    start, end = extract_folio_range(filename)
    if start is None or end is None:
        # Arquivos sem sufixo de folha vao para o final, mantendo ordem de entrada.
        return (1, 10**9, 10**9, fallback_index)
    return (0, start, end, fallback_index)


def parse_folio_selection_ranges(raw_text: str) -> list[tuple[int, int]]:
    tokens = [token.strip() for token in re.split(r"[,\n;]+", raw_text) if token.strip()]
    if not tokens:
        return []

    ranges: list[tuple[int, int]] = []
    for token in tokens:
        match = FOLIO_SELECTION_TOKEN_RE.match(token)
        if not match:
            raise ValueError(f"Faixa invalida: '{token}'. Use formato 10 ou 10-25.")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            raise ValueError(f"Faixa invalida: '{token}'.")
        ranges.append((start, end))
    return ranges


def piece_intersects_folio_ranges(
    folio_start: int | None,
    folio_end: int | None,
    folio_ranges: list[tuple[int, int]],
) -> bool:
    if folio_start is None or folio_end is None:
        return False
    for range_start, range_end in folio_ranges:
        if folio_end < range_start:
            continue
        if folio_start > range_end:
            continue
        return True
    return False


def folio_in_ranges(
    folio: int | None,
    folio_ranges: list[tuple[int, int]] | None,
) -> bool:
    if folio is None or folio <= 0 or not folio_ranges:
        return False
    for range_start, range_end in folio_ranges:
        if range_start <= folio <= range_end:
            return True
    return False


def group_contiguous_page_indexes(page_indexes: list[int]) -> list[list[int]]:
    normalized = sorted({int(page_index) for page_index in page_indexes if int(page_index) >= 0})
    if not normalized:
        return []

    groups: list[list[int]] = []
    current_group = [normalized[0]]
    for page_index in normalized[1:]:
        if page_index == current_group[-1] + 1:
            current_group.append(page_index)
            continue
        groups.append(current_group)
        current_group = [page_index]
    groups.append(current_group)
    return groups


def format_folio_ranges_for_observation(folios: list[int]) -> str:
    normalized = sorted({int(folio) for folio in folios if int(folio) > 0})
    if not normalized:
        return ""

    parts: list[str] = []
    start = normalized[0]
    end = normalized[0]
    for folio in normalized[1:]:
        if folio == end + 1:
            end = folio
            continue
        parts.append(f"{start}-{end}" if start != end else str(start))
        start = folio
        end = folio
    parts.append(f"{start}-{end}" if start != end else str(start))
    return ", ".join(parts)


def _pdf_escape_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ensure_folio_font(page: Any, writer: PdfWriter) -> NameObject:
    resources = page.get(NameObject("/Resources"))
    if resources is None:
        resources = DictionaryObject()
        page[NameObject("/Resources")] = resources
    elif hasattr(resources, "get_object"):
        resources = resources.get_object()

    fonts = resources.get(NameObject("/Font"))
    if fonts is None:
        fonts = DictionaryObject()
        resources[NameObject("/Font")] = fonts
    elif hasattr(fonts, "get_object"):
        fonts = fonts.get_object()

    font_key = NameObject("/FLSB")
    if font_key not in fonts:
        font_obj = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica-Bold"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        fonts[font_key] = writer._add_object(font_obj)  # noqa: SLF001 - necessario para PDF compacto
    return font_key


def _append_folio_to_page_content(page: Any, writer: PdfWriter, label: str, width: float, height: float) -> None:
    font_size = 10.5
    margin_x = 12.0
    box_height = 16.0
    box_padding = 3.0
    text_x = margin_x + box_padding

    # Fundo branco discreto para legibilidade do marcador AI_PAGE.
    estimated_char_width = font_size * 0.57
    text_width = max(24.0, len(label) * estimated_char_width)
    box_width = max(120.0, min(320.0, text_width + (box_padding * 2.0)))
    box_top = height - 8.0
    box_bottom = max(0.0, box_top - box_height)
    if margin_x + box_width > width - 4.0:
        box_width = max(40.0, width - margin_x - 4.0)

    baseline_y = box_bottom + 3.5
    font_name = _ensure_folio_font(page, writer)
    safe_label = _pdf_escape_text(label)
    ops = (
        "q\n"
        "1 1 1 rg\n"
        f"{margin_x:.2f} {box_bottom:.2f} {box_width:.2f} {box_height:.2f} re f\n"
        "BT\n"
        "0 0 0 rg\n"
        f"{font_name} {font_size:.2f} Tf\n"
        f"1 0 0 1 {text_x:.2f} {baseline_y:.2f} Tm\n"
        f"({safe_label}) Tj\n"
        "ET\n"
        "Q\n"
    )

    stream = DecodedStreamObject()
    stream.set_data(ops.encode("latin-1", "replace"))
    stream_ref = writer._add_object(stream)  # noqa: SLF001 - necessario para PDF compacto

    current_contents = page.get(NameObject("/Contents"))
    if current_contents is None:
        page[NameObject("/Contents")] = stream_ref
        return

    if isinstance(current_contents, ArrayObject):
        current_contents.append(stream_ref)
        return

    if hasattr(current_contents, "get_object"):
        current_obj = current_contents.get_object()
        if isinstance(current_obj, ArrayObject):
            current_obj.append(stream_ref)
            return

    page[NameObject("/Contents")] = ArrayObject([current_contents, stream_ref])


def _marker_kind_label(marker_kind: str) -> str:
    if marker_kind == PHOTO_MARKER_KIND_WITH_TEXT:
        return "FOTOGRAFIA COM TEXTO (WHATSAPP/DOCUMENTO)"
    return "FOTOGRAFIA PURA"


def _merge_piece_observation(base: str, extra: str) -> str:
    base_text = str(base or "").strip()
    extra_text = str(extra or "").strip()
    if base_text and extra_text:
        return f"{base_text}; {extra_text}"
    return base_text or extra_text


def _append_photo_placeholder_to_page_content(page: Any, writer: PdfWriter, marker_kind: str, source_name: str) -> None:
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    margin = max(18.0, min(42.0, width * 0.04))
    box_width = max(140.0, width - (2.0 * margin))
    box_height = min(max(130.0, height * 0.30), max(130.0, height - (2.0 * margin)))
    box_x = (width - box_width) / 2.0
    box_y = (height - box_height) / 2.0

    title = "PAGINA SUBSTITUIDA"
    marker_label = _marker_kind_label(marker_kind)
    marker_tag = f"[[AI_IMAGE: {marker_kind}]]"
    source_label = f"Arquivo original: {Path(source_name).name}"
    source_label = source_label[:120]

    font_name = _ensure_folio_font(page, writer)

    def centered_x(text: str, size: float) -> float:
        estimated_width = len(text) * size * 0.54
        return max(margin + 8.0, min(width - margin - estimated_width, (width - estimated_width) / 2.0))

    line1_y = box_y + box_height - 44.0
    line2_y = line1_y - 30.0
    line3_y = line2_y - 24.0
    line4_y = max(box_y + 18.0, line3_y - 22.0)

    ops = (
        "q\n"
        "1 1 1 rg\n"
        f"0.00 0.00 {width:.2f} {height:.2f} re f\n"
        "0.93 0.93 0.93 rg\n"
        f"{box_x:.2f} {box_y:.2f} {box_width:.2f} {box_height:.2f} re f\n"
        "0.45 0.45 0.45 RG\n"
        "1 w\n"
        f"{box_x:.2f} {box_y:.2f} {box_width:.2f} {box_height:.2f} re S\n"
        "BT\n"
        "0 0 0 rg\n"
        f"{font_name} 20 Tf\n"
        f"1 0 0 1 {centered_x(title, 20.0):.2f} {line1_y:.2f} Tm\n"
        f"({_pdf_escape_text(title)}) Tj\n"
        "ET\n"
        "BT\n"
        "0 0 0 rg\n"
        f"{font_name} 13 Tf\n"
        f"1 0 0 1 {centered_x(marker_label, 13.0):.2f} {line2_y:.2f} Tm\n"
        f"({_pdf_escape_text(marker_label)}) Tj\n"
        "ET\n"
        "BT\n"
        "0 0 0 rg\n"
        f"{font_name} 10.8 Tf\n"
        f"1 0 0 1 {centered_x(marker_tag, 10.8):.2f} {line3_y:.2f} Tm\n"
        f"({_pdf_escape_text(marker_tag)}) Tj\n"
        "ET\n"
        "BT\n"
        "0 0 0 rg\n"
        f"{font_name} 9.2 Tf\n"
        f"1 0 0 1 {margin + 8.0:.2f} {line4_y:.2f} Tm\n"
        f"({_pdf_escape_text(source_label)}) Tj\n"
        "ET\n"
        "Q\n"
    )

    stream = DecodedStreamObject()
    stream.set_data(ops.encode("latin-1", "replace"))
    stream_ref = writer._add_object(stream)  # noqa: SLF001 - necessario para PDF compacto
    page[NameObject("/Contents")] = stream_ref


def _add_photo_placeholder_page(writer: PdfWriter, source_page: Any, marker_kind: str, source_name: str) -> None:
    width = float(source_page.mediabox.width)
    height = float(source_page.mediabox.height)
    if width <= 0 or height <= 0:
        width = 595.0
        height = 842.0
    placeholder_page = writer.add_blank_page(width=width, height=height)
    _append_photo_placeholder_to_page_content(placeholder_page, writer, marker_kind, source_name)


def _crop_right_margin(page: Any, width_points: float) -> bool:
    try:
        crop_points = float(width_points)
    except Exception:
        return False
    if crop_points <= 0:
        return False

    left = float(page.mediabox.left)
    right = float(page.mediabox.right)
    top = float(page.mediabox.top)
    bottom = float(page.mediabox.bottom)
    page_width = right - left
    if page_width <= 0:
        return False

    # Mantem uma largura minima para evitar paginas invalidas.
    min_width = max(72.0, page_width * 0.45)
    max_crop = max(0.0, page_width - min_width)
    effective_crop = min(crop_points, max_crop)
    if effective_crop <= 0.1:
        return False

    new_right = right - effective_crop

    page.mediabox.lower_left = (left, bottom)
    page.mediabox.upper_right = (new_right, top)
    page.cropbox.lower_left = (left, bottom)
    page.cropbox.upper_right = (new_right, top)

    for box_name in ("trimbox", "artbox", "bleedbox"):
        try:
            box = getattr(page, box_name)
            box.lower_left = (left, bottom)
            box.upper_right = (new_right, top)
        except Exception:
            continue
    return True


def map_piece_folios(piece: PiecePdfInput, page_count: int) -> tuple[list[int | None], str, list[dict[str, str | int]]]:
    start = piece.folio_start
    end = piece.folio_end
    incons_rows: list[dict[str, str | int]] = []

    if start is None or end is None:
        incons_rows.append(
            {
                "ordem_compilacao": piece.source_index,
                "categoria": piece.category,
                "arquivo_original": piece.original_name,
                "fls_inicial_original": "",
                "fls_final_original": "",
                "paginas_pdf_fonte": page_count,
                "tipo_inconsistencia": "sem_faixa_folhas",
                "detalhe": "Nao foi possivel identificar faixa de folhas no nome do arquivo.",
            }
        )
        return [None] * page_count, "sem_faixa_folhas", incons_rows

    expected_pages = (end - start) + 1
    if expected_pages <= 0:
        incons_rows.append(
            {
                "ordem_compilacao": piece.source_index,
                "categoria": piece.category,
                "arquivo_original": piece.original_name,
                "fls_inicial_original": start,
                "fls_final_original": end,
                "paginas_pdf_fonte": page_count,
                "tipo_inconsistencia": "faixa_invalida",
                "detalhe": "Faixa de folhas invalida no nome do arquivo.",
            }
        )
        return [None] * page_count, "faixa_invalida", incons_rows

    if page_count == expected_pages:
        return [start + offset for offset in range(page_count)], "", []

    if page_count < expected_pages:
        incons_rows.append(
            {
                "ordem_compilacao": piece.source_index,
                "categoria": piece.category,
                "arquivo_original": piece.original_name,
                "fls_inicial_original": start,
                "fls_final_original": end,
                "paginas_pdf_fonte": page_count,
                "tipo_inconsistencia": "pdf_menor_que_faixa",
                "detalhe": f"PDF possui {page_count} paginas e a faixa indica {expected_pages}.",
            }
        )
        return [start + offset for offset in range(page_count)], "pdf_menor_que_faixa", incons_rows

    incons_rows.append(
        {
            "ordem_compilacao": piece.source_index,
            "categoria": piece.category,
            "arquivo_original": piece.original_name,
            "fls_inicial_original": start,
            "fls_final_original": end,
            "paginas_pdf_fonte": page_count,
            "tipo_inconsistencia": "pdf_maior_que_faixa",
            "detalhe": f"PDF possui {page_count} paginas e a faixa indica {expected_pages}. Paginas excedentes ficaram como fls. NA.",
        }
    )
    mapped = [start + offset for offset in range(expected_pages)] + [None] * (page_count - expected_pages)
    return mapped, "pdf_maior_que_faixa", incons_rows


def write_summary_markdown(path: Path, rows: list[dict[str, str | int]]) -> None:
    lines = [
        "# Sumario de Pecas Principais",
        "",
        "| Ordem | Categoria | Arquivo Original | Fls Original | Paginas no PDF IA | Observacao |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        fls_inicial = row.get("fls_inicial_original") or "-"
        fls_final = row.get("fls_final_original") or "-"
        fls_text = f"{fls_inicial}-{fls_final}" if fls_inicial != "-" else "-"
        pag_ini = row.get("pagina_inicial_pdf_ia") or "-"
        pag_fim = row.get("pagina_final_pdf_ia") or "-"
        pag_text = f"{pag_ini}-{pag_fim}" if pag_ini != "-" else "-"
        obs = str(row.get("observacao", "") or "-")
        categoria = str(row.get("categoria", ""))
        nome = str(row.get("arquivo_original", ""))
        ordem = str(row.get("ordem_compilacao", ""))
        lines.append(f"| {ordem} | {categoria} | {nome} | {fls_text} | {pag_text} | {obs} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _format_aip_marker(value: Any) -> str:
    parsed = _to_int(value)
    if parsed is None or parsed <= 0:
        return ""
    return f"[AIP: {parsed}]"


def _compact_full_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    aip_inicio = str(row.get("aip_inicio") or "").strip()
    aip_fim = str(row.get("aip_fim") or "").strip()
    if not aip_inicio:
        aip_inicio = _format_aip_marker(row.get("pagina_inicial_pdf_ia") or row.get("fls_inicial_original"))
    if not aip_fim:
        aip_fim = _format_aip_marker(row.get("pagina_final_pdf_ia") or row.get("fls_final_original"))
    return {
        "indice": row.get("indice", ""),
        "arquivo_original": row.get("arquivo_original", ""),
        "categoria": row.get("categoria", ""),
        "aip_inicio": aip_inicio,
        "aip_fim": aip_fim,
    }


def _compact_selected_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    aip_inicio = _format_aip_marker(row.get("pagina_inicial_pdf_ia"))
    aip_fim = _format_aip_marker(row.get("pagina_final_pdf_ia"))
    return {
        "indice": row.get("ordem_compilacao", ""),
        "arquivo_original": row.get("arquivo_original", ""),
        "categoria": row.get("categoria", ""),
        "aip_inicio": aip_inicio,
        "aip_fim": aip_fim,
    }


def _build_public_full_summary_payload(
    *,
    now_iso: str,
    zip_filename: str,
    modo_processamento: str,
    selection_criteria: str,
    total_nao_fornecidas: int,
    max_folio_in_process: int,
    inconsistency_rows: list[dict[str, Any]],
    full_summary_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "versao": "1.0",
        "gerado_em": now_iso,
        "arquivo_zip": zip_filename,
        "modo_processamento": modo_processamento,
        "criterio_selecao_pecas_principais": selection_criteria,
        "lacunas_existentes": total_nao_fornecidas > 0,
        "total_nao_fornecidas": total_nao_fornecidas,
        "total_folhas_processo": max_folio_in_process,
        "total_inconsistencias_paginas": len(inconsistency_rows),
        "total_itens": len(full_summary_rows),
        "itens": [_compact_full_summary_row(row) for row in full_summary_rows],
    }


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _estimated_piece_folios(piece: PiecePdfInput, summary_row: dict[str, str | int] | None) -> int:
    if piece.folio_start is not None and piece.folio_end is not None and piece.folio_end >= piece.folio_start:
        return max(1, (piece.folio_end - piece.folio_start) + 1)

    if summary_row is not None:
        pages = _to_int(summary_row.get("paginas_pdf_fonte"))
        if pages is not None and pages > 0:
            return pages

    return 1


def _estimated_piece_size_bytes(piece: PiecePdfInput) -> int:
    try:
        return max(0, int(piece.pdf_path.stat().st_size))
    except Exception:
        return 0


def _estimated_piece_tokens(piece: PiecePdfInput, summary_row: dict[str, str | int] | None) -> int:
    folios = _estimated_piece_folios(piece, summary_row)
    return max(1, folios * ESTIMATED_TOKENS_PER_FOLIO)


def _normalize_batch_cut_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == BATCH_CUT_MODE_TOKENS:
        return BATCH_CUT_MODE_TOKENS
    return BATCH_CUT_MODE_MB


def _split_pieces_by_targets(
    pieces: list[PiecePdfInput],
    summary_by_index: dict[int, dict[str, str | int]],
    batch_cut_mode: str,
    max_batch_size_bytes: int | None,
    batch_target_tokens: int,
    batch_min_tokens: int,
    batch_max_tokens: int,
) -> list[tuple[list[PiecePdfInput], int, int, int]]:
    if not pieces:
        return []

    batches: list[tuple[list[PiecePdfInput], int, int, int]] = []
    current_batch: list[PiecePdfInput] = []
    current_estimated_folios = 0
    current_estimated_size_bytes = 0
    current_estimated_tokens = 0

    for piece in pieces:
        summary_row = summary_by_index.get(piece.source_index)
        estimated_piece_folios = _estimated_piece_folios(piece, summary_row)
        estimated_piece_size_bytes = _estimated_piece_size_bytes(piece)
        estimated_piece_tokens = _estimated_piece_tokens(piece, summary_row)

        should_split = False
        if current_batch:
            if batch_cut_mode == BATCH_CUT_MODE_TOKENS:
                next_tokens = current_estimated_tokens + estimated_piece_tokens
                if next_tokens > batch_max_tokens:
                    should_split = True
                elif current_estimated_tokens >= batch_min_tokens and next_tokens > batch_target_tokens:
                    should_split = True
            else:
                should_split = (
                    max_batch_size_bytes is not None
                    and (current_estimated_size_bytes + estimated_piece_size_bytes > max_batch_size_bytes)
                )

        if should_split:
            batches.append((current_batch, current_estimated_folios, current_estimated_size_bytes, current_estimated_tokens))
            current_batch = []
            current_estimated_folios = 0
            current_estimated_size_bytes = 0
            current_estimated_tokens = 0

        current_batch.append(piece)
        current_estimated_folios += estimated_piece_folios
        current_estimated_size_bytes += estimated_piece_size_bytes
        current_estimated_tokens += estimated_piece_tokens

    if current_batch:
        batches.append((current_batch, current_estimated_folios, current_estimated_size_bytes, current_estimated_tokens))

    return batches


def generate_ia_batches(
    *,
    pieces: list[PiecePdfInput],
    selected_summary_rows: list[dict[str, str | int]],
    total_folios: int,
    output_root: Path,
    zip_filename: str,
    selection_criteria: str,
    batch_max_size_mb: int,
    batch_cut_mode: str = DEFAULT_BATCH_CUT_MODE,
    batch_target_tokens: int = DEFAULT_BATCH_TARGET_TOKENS,
    batch_min_tokens: int = DEFAULT_BATCH_MIN_TOKENS,
    batch_max_tokens: int = DEFAULT_BATCH_MAX_TOKENS,
    replace_photo_pages: bool = True,
    crop_signature_strip_right: bool = False,
    crop_signature_strip_width_points: float = DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS,
    emit: Callable[[str], None] | None = None,
) -> tuple[Path | None, Path | None, int]:
    batches_root = output_root / "lotes_ia"
    if batches_root.exists():
        shutil.rmtree(batches_root)

    def _emit(message: str) -> None:
        if emit:
            emit(message)

    if not pieces:
        _emit("Loteamento IA ignorado: nenhuma peca PDF principal disponivel.")
        return None, None, 0

    artifacts_root = batches_root / "arquivos_gerados"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    target_size_mb = max(0, batch_max_size_mb)
    normalized_cut_mode = _normalize_batch_cut_mode(batch_cut_mode)
    normalized_target_tokens = max(1, int(batch_target_tokens or DEFAULT_BATCH_TARGET_TOKENS))
    normalized_min_tokens = max(1, int(batch_min_tokens or DEFAULT_BATCH_MIN_TOKENS))
    normalized_max_tokens = max(normalized_target_tokens, int(batch_max_tokens or DEFAULT_BATCH_MAX_TOKENS))
    if normalized_min_tokens > normalized_target_tokens:
        normalized_min_tokens = normalized_target_tokens
    if normalized_target_tokens > normalized_max_tokens:
        normalized_target_tokens = normalized_max_tokens

    max_batch_size_bytes = target_size_mb * 1024 * 1024 if target_size_mb > 0 else None
    ordered_pieces = sorted(pieces, key=lambda item: item.order_key)
    summary_by_index: dict[int, dict[str, str | int]] = {}
    for row in selected_summary_rows:
        idx = _to_int(row.get("ordem_compilacao"))
        if idx is not None:
            summary_by_index[idx] = row

    split_batches = _split_pieces_by_targets(
        ordered_pieces,
        summary_by_index,
        normalized_cut_mode,
        max_batch_size_bytes,
        normalized_target_tokens,
        normalized_min_tokens,
        normalized_max_tokens,
    )
    if normalized_cut_mode == BATCH_CUT_MODE_TOKENS:
        _emit(
            "Loteamento IA: "
            f"{len(split_batches)} lote(s) com alvo ~{normalized_target_tokens} tokens "
            f"(faixa {normalized_min_tokens}-{normalized_max_tokens})."
        )
    elif max_batch_size_bytes is not None:
        _emit(f"Loteamento IA: {len(split_batches)} lote(s) com limite de ~{target_size_mb} MB por lote.")
    else:
        _emit("Loteamento IA: sem limite de tamanho configurado (lote unico).")

    lot_rows: list[dict[str, Any]] = []
    total_items = 0
    total_pages = 0
    total_inconsistencies = 0
    total_estimated_bytes = 0
    total_estimated_tokens = 0
    now_iso = datetime.now().isoformat(timespec="seconds")

    for lot_number, (lot_pieces, estimated_folios, estimated_size_bytes, estimated_tokens) in enumerate(split_batches, start=1):
        lot_name = build_filter_lot_name(lot_number)
        lot_piece_root = batches_root / lot_name / "pecas"
        lot_piece_root.mkdir(parents=True, exist_ok=True)

        copied_piece_destinations: dict[int, str] = {}
        for piece in lot_pieces:
            safe_category = sanitize_component(piece.category, "SemCategoria")
            destination = unique_path(lot_piece_root / safe_category / piece.pdf_path.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(piece.pdf_path, destination)
            copied_piece_destinations[piece.source_index] = str(destination.relative_to(output_root))

        lot_pdf_path = artifacts_root / build_filter_lot_pdf_name(lot_number)
        lot_summary_path = artifacts_root / build_filter_lot_summary_json_name(lot_number)
        (
            generated_lot_pdf_path,
            lot_pdf_inputs,
            lot_pdf_skipped,
            lot_total_pages,
            lot_summary_rows,
            lot_inconsistency_rows,
        ) = build_ia_pdf_and_reports(
            lot_pieces,
            total_folios,
            lot_pdf_path,
            None,
            None,
            replace_photo_pages=replace_photo_pages,
            crop_signature_strip_right=crop_signature_strip_right,
            crop_signature_strip_width_points=crop_signature_strip_width_points,
        )

        for row in lot_summary_rows:
            idx = _to_int(row.get("ordem_compilacao"))
            if idx is None:
                row["destino_pasta_pecas"] = ""
            else:
                row["destino_pasta_pecas"] = copied_piece_destinations.get(idx, "")

        write_json(
            lot_summary_path,
            {
                "versao": "1.0",
                "gerado_em": now_iso,
                "arquivo_zip": zip_filename,
                "criterio_selecao_pecas_principais": selection_criteria,
                "lote": lot_name,
                "indice_lote": lot_number,
                "total_lotes_previstos": len(split_batches),
                "modo_corte_lote": normalized_cut_mode,
                "target_tamanho_mb_lote": target_size_mb,
                "target_tamanho_bytes_lote": max_batch_size_bytes or 0,
                "target_tokens_lote": normalized_target_tokens,
                "target_tokens_min_lote": normalized_min_tokens,
                "target_tokens_max_lote": normalized_max_tokens,
                "folhas_estimadas_lote": estimated_folios,
                "tamanho_estimado_lote_bytes": estimated_size_bytes,
                "tokens_estimados_lote": estimated_tokens,
                "limite_tamanho_excedido_no_lote": bool(
                    max_batch_size_bytes is not None and estimated_size_bytes > max_batch_size_bytes
                ),
                "limite_tokens_excedido_no_lote": bool(
                    normalized_cut_mode == BATCH_CUT_MODE_TOKENS and estimated_tokens > normalized_max_tokens
                ),
                "total_documentos_pdf_validos": lot_pdf_inputs,
                "total_documentos_pdf_ignorados": lot_pdf_skipped,
                "total_paginas_pdf_lote": lot_total_pages,
                "total_inconsistencias_paginas": len(lot_inconsistency_rows),
                "total_itens": len(lot_summary_rows),
                "itens": lot_summary_rows,
            },
        )

        total_items += len(lot_summary_rows)
        total_pages += lot_total_pages
        total_inconsistencies += len(lot_inconsistency_rows)
        total_estimated_bytes += estimated_size_bytes
        total_estimated_tokens += estimated_tokens
        lot_rows.append(
            {
                "lote": lot_name,
                "indice_lote": lot_number,
                "pasta_pecas_lote": str((batches_root / lot_name / "pecas").relative_to(output_root)),
                "arquivo_pdf_lote": (
                    str(generated_lot_pdf_path.relative_to(output_root))
                    if generated_lot_pdf_path is not None
                    else ""
                ),
                "sumario_json_lote": str(lot_summary_path.relative_to(output_root)),
                "folhas_estimadas_lote": estimated_folios,
                "tamanho_estimado_lote_bytes": estimated_size_bytes,
                "tokens_estimados_lote": estimated_tokens,
                "limite_tamanho_excedido_no_lote": bool(
                    max_batch_size_bytes is not None and estimated_size_bytes > max_batch_size_bytes
                ),
                "limite_tokens_excedido_no_lote": bool(
                    normalized_cut_mode == BATCH_CUT_MODE_TOKENS and estimated_tokens > normalized_max_tokens
                ),
                "total_itens": len(lot_summary_rows),
                "total_paginas_pdf_lote": lot_total_pages,
                "total_inconsistencias_paginas": len(lot_inconsistency_rows),
            }
        )
        _emit(f"Lote {lot_number}/{len(split_batches)} gerado: {len(lot_summary_rows)} peca(s).")

    full_summary_path = artifacts_root / FILTER_BATCH_FULL_SUMMARY_JSON_NAME
    write_json(
        full_summary_path,
        {
            "versao": "1.0",
            "gerado_em": now_iso,
            "arquivo_zip": zip_filename,
            "criterio_selecao_pecas_principais": selection_criteria,
            "modo_corte_lote": normalized_cut_mode,
            "target_tamanho_mb_lote": target_size_mb,
            "target_tamanho_bytes_lote": max_batch_size_bytes or 0,
            "target_tokens_lote": normalized_target_tokens,
            "target_tokens_min_lote": normalized_min_tokens,
            "target_tokens_max_lote": normalized_max_tokens,
            "total_lotes": len(lot_rows),
            "total_itens": total_items,
            "total_paginas_pdf_lotes": total_pages,
            "total_tamanho_estimado_lotes_bytes": total_estimated_bytes,
            "total_tokens_estimados_lotes": total_estimated_tokens,
            "total_inconsistencias_paginas": total_inconsistencies,
            "lotes": lot_rows,
        },
    )
    _emit("Sumario completo de lotes gerado.")
    return batches_root, full_summary_path, len(lot_rows)


def build_ia_pdf_and_reports(
    input_pieces: list[PiecePdfInput],
    total_folios: int,
    pdf_destination: Path,
    selected_summary_json_path: Path | None,
    selected_summary_md_path: Path | None,
    *,
    selected_folio_ranges: list[tuple[int, int]] | None = None,
    replace_photo_pages: bool = True,
    crop_signature_strip_right: bool = False,
    crop_signature_strip_width_points: float = DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS,
) -> tuple[Path | None, int, int, int, list[dict[str, str | int]], list[dict[str, str | int]]]:
    writer = PdfWriter()
    included_docs = 0
    skipped_docs = 0
    output_page_cursor = 0
    output_page_folios: list[int | None] = []
    summary_rows: list[dict[str, str | int]] = []
    inconsistency_rows: list[dict[str, str | int]] = []

    for piece in sorted(input_pieces, key=lambda item: item.order_key):
        try:
            reader = PdfReader(str(piece.pdf_path))
        except Exception as exc:
            skipped_docs += 1
            summary_rows.append(
                {
                    "ordem_compilacao": piece.source_index,
                    "categoria": piece.category,
                    "arquivo_original": piece.original_name,
                    "fls_inicial_original": piece.folio_start or "",
                    "fls_final_original": piece.folio_end or "",
                    "paginas_pdf_fonte": 0,
                    "pagina_inicial_pdf_ia": "",
                    "pagina_final_pdf_ia": "",
                    "observacao": f"erro_leitura_pdf: {exc}",
                }
            )
            inconsistency_rows.append(
                {
                    "ordem_compilacao": piece.source_index,
                    "categoria": piece.category,
                    "arquivo_original": piece.original_name,
                    "fls_inicial_original": piece.folio_start or "",
                    "fls_final_original": piece.folio_end or "",
                    "paginas_pdf_fonte": 0,
                    "tipo_inconsistencia": "erro_leitura_pdf",
                    "detalhe": str(exc),
                }
            )
            continue

        page_count = len(reader.pages)
        _mapped_folios, obs, piece_inconsistency_rows = map_piece_folios(piece, page_count)
        inconsistency_rows.extend(piece_inconsistency_rows)
        marker_kind = detect_photo_marker_kind(piece) if replace_photo_pages else None
        photo_note = ""
        if marker_kind is not None and page_count > 0:
            photo_note = (
                f"{marker_kind}; paginas_substituidas={page_count}; "
                "original_excluido=true"
            )
        merged_obs = _merge_piece_observation(obs, photo_note)
        selected_page_indexes = list(range(page_count))
        if selected_folio_ranges:
            selected_page_indexes = [
                page_index
                for page_index, mapped_folio in enumerate(_mapped_folios)
                if folio_in_ranges(mapped_folio if isinstance(mapped_folio, int) else None, selected_folio_ranges)
            ]

        page_groups = group_contiguous_page_indexes(selected_page_indexes)
        if not page_groups:
            continue

        for page_group in page_groups:
            selected_folios = [
                _mapped_folios[page_index]
                for page_index in page_group
                if page_index < len(_mapped_folios) and isinstance(_mapped_folios[page_index], int) and _mapped_folios[page_index] > 0
            ]
            if selected_folios:
                page_start_out = selected_folios[0]
                page_end_out = selected_folios[-1]
            else:
                page_start_out = output_page_cursor + 1 if page_group else ""
                page_end_out = output_page_cursor + len(page_group) if page_group else ""

            segment_obs = merged_obs
            if selected_folio_ranges:
                selected_folios_text = format_folio_ranges_for_observation(selected_folios)
                if selected_folios_text:
                    segment_obs = _merge_piece_observation(
                        segment_obs,
                        f"recorte_exato_folhas={selected_folios_text}",
                    )

            summary_rows.append(
                {
                    "ordem_compilacao": piece.source_index,
                    "categoria": piece.category,
                    "arquivo_original": piece.original_name,
                    "fls_inicial_original": piece.folio_start or "",
                    "fls_final_original": piece.folio_end or "",
                    "paginas_pdf_fonte": len(page_group),
                    "pagina_inicial_pdf_ia": page_start_out,
                    "pagina_final_pdf_ia": page_end_out,
                    "observacao": segment_obs,
                }
            )

            for page_index in page_group:
                page = reader.pages[page_index]
                if marker_kind is None:
                    writer.add_page(page)
                else:
                    _add_photo_placeholder_page(writer, page, marker_kind, piece.original_name)
                added_page = writer.pages[-1]
                if crop_signature_strip_right:
                    _crop_right_margin(added_page, crop_signature_strip_width_points)
                mapped_folio = _mapped_folios[page_index] if page_index < len(_mapped_folios) else None
                output_page_folios.append(mapped_folio)
                output_page_cursor += 1

        included_docs += 1

    now_iso = datetime.now().isoformat(timespec="seconds")
    if selected_summary_json_path is not None:
        write_json(
            selected_summary_json_path,
            {
                "versao": "1.0",
                "gerado_em": now_iso,
                "total_folhas_processo": total_folios,
                "total_itens": len(summary_rows),
                "itens": [_compact_selected_summary_row(row) for row in summary_rows],
            },
        )

    if selected_summary_md_path is not None:
        write_summary_markdown(selected_summary_md_path, summary_rows)

    if output_page_cursor == 0:
        return None, included_docs, skipped_docs, 0, summary_rows, inconsistency_rows

    total_ia_pages = len(writer.pages)
    known_folios = [folio for folio in output_page_folios if isinstance(folio, int) and folio > 0]
    for page_number, target_page in enumerate(writer.pages, start=1):
        mapped_folio = output_page_folios[page_number - 1] if page_number - 1 < len(output_page_folios) else None
        label_page = mapped_folio if isinstance(mapped_folio, int) and mapped_folio > 0 else page_number
        label = f"[AIP: {label_page}]"
        width = float(target_page.mediabox.width)
        height = float(target_page.mediabox.height)
        _append_folio_to_page_content(target_page, writer, label, width, height)
        try:
            target_page.compress_content_streams()
        except Exception:
            pass

    # Nao aplicar deduplicacao global de objetos: em alguns processos grandes,
    # essa etapa pode gerar PDF estruturalmente invalido e corromper a extracao de texto.

    pdf_destination.parent.mkdir(parents=True, exist_ok=True)
    with pdf_destination.open("wb") as target:
        writer.write(target)

    return pdf_destination, included_docs, skipped_docs, output_page_cursor, summary_rows, inconsistency_rows


def process_zip(config: ProcessingConfig, log: Callable[[str], None] | None = None) -> ProcessingResult:
    def emit(message: str) -> None:
        if log:
            log(message)

    if not config.zip_path.exists():
        raise FileNotFoundError(f"ZIP nao encontrado: {config.zip_path}")

    base_output_name = sanitize_component(config.zip_path.stem, "processo") + "_organizado"
    output_root = config.output_dir / base_output_name
    main_root = output_root / "01_pecas_principais"
    auxiliary_root = output_root / "02_documentos_auxiliares"
    full_summary_json_path = output_root / FILTER_FULL_SUMMARY_JSON_NAME
    full_summary_runtime_json_path = output_root / FILTER_FULL_SUMMARY_RUNTIME_JSON_NAME
    output_root.mkdir(parents=True, exist_ok=True)

    if config.replace_photo_pages:
        emit(
            "Filtro de fotografias ativo: paginas classificadas como fotografia "
            "serao substituidas por marcador mantendo a paginacao."
        )
    else:
        emit("Filtro de fotografias desativado: paginas originais serao mantidas no PDF IA.")
    if config.crop_signature_strip_right:
        emit(
            "Recorte de tarja lateral ativo: "
            f"corte da direita em {float(config.crop_signature_strip_width_points):.1f} pt por pagina."
        )
    else:
        emit("Recorte de tarja lateral desativado.")

    if config.integral_mode:
        emit("Processamento integral (MONK): gerando processo completo...")
        integral_folder = output_root / "processo_completo"
        integral_pdf_path = output_root / FILTER_FULL_PROCESS_PDF_NAME
        integral_summary_path = integral_folder / FILTER_FULL_SUMMARY_JSON_NAME
        integral_summary_runtime_path = integral_folder / FILTER_FULL_SUMMARY_RUNTIME_JSON_NAME

        if integral_folder.exists():
            shutil.rmtree(integral_folder)
        integral_folder.mkdir(parents=True, exist_ok=True)

        total_files = 0
        main_files = 0
        auxiliary_files = 0
        max_folio_in_process = 0
        main_pdf_inputs: list[PiecePdfInput] = []
        full_summary_rows: list[dict[str, str | int]] = []

        emit("Abrindo ZIP...")
        with zipfile.ZipFile(config.zip_path, "r") as zf:
            entries = [entry for entry in zf.infolist() if not entry.is_dir()]
            emit(f"{len(entries)} arquivos localizados no ZIP.")

            for idx, entry in enumerate(entries, start=1):
                original_name = entry.filename
                total_files += 1
                folio_start, folio_end = extract_folio_range(original_name)
                if folio_end is not None:
                    max_folio_in_process = max(max_folio_in_process, folio_end)

                category = base_category_from_name(original_name)
                safe_file_name = sanitize_component(Path(original_name).name, "arquivo")
                destination = unique_path(integral_folder / category / safe_file_name)
                destination.parent.mkdir(parents=True, exist_ok=True)

                with zf.open(entry, "r") as source:
                    with destination.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)

                main_files += 1
                if destination.suffix.lower() == ".pdf":
                    main_pdf_inputs.append(
                        PiecePdfInput(
                            order_key=page_sort_key(original_name, idx),
                            source_index=idx,
                            original_name=original_name,
                            category=category,
                            pdf_path=destination,
                            folio_start=folio_start,
                            folio_end=folio_end,
                        )
                    )

                full_summary_rows.append(
                {
                    "indice": idx,
                    "arquivo_original": original_name,
                    "categoria": category,
                        "extensao": destination.suffix.lower(),
                        "fls_inicial_original": folio_start or "",
                        "fls_final_original": folio_end or "",
                        "status_fornecimento": "selecionada",
                        "pasta_mestra": "processo_completo",
                        "regra_classificacao": "processamento_integral",
                    "destino": str(destination.relative_to(output_root)),
                    "pagina_inicial_pdf_ia": "",
                    "pagina_final_pdf_ia": "",
                    "aip_inicio": _format_aip_marker(folio_start),
                    "aip_fim": _format_aip_marker(folio_end or folio_start),
                    "observacao_pdf_ia": "",
                }
            )

                if idx % 20 == 0 or idx == len(entries):
                    emit(f"Processados {idx}/{len(entries)} arquivos...")

        emit(msg_filter_generating_full_pdf())
        ia_pdf_path, ia_pdf_inputs, ia_pdf_skipped, ia_pdf_total_pages, selected_summary_rows, inconsistency_rows = build_ia_pdf_and_reports(
            main_pdf_inputs,
            max_folio_in_process,
            integral_pdf_path,
            None,
            None,
            replace_photo_pages=config.replace_photo_pages,
            crop_signature_strip_right=config.crop_signature_strip_right,
            crop_signature_strip_width_points=config.crop_signature_strip_width_points,
        )

        selected_summary_by_index: dict[int, dict[str, str | int]] = {}
        for row in selected_summary_rows:
            idx_raw = row.get("ordem_compilacao")
            if isinstance(idx_raw, int):
                selected_summary_by_index[idx_raw] = row

        for row in full_summary_rows:
            idx_raw = row.get("indice")
            if not isinstance(idx_raw, int):
                continue
            selected_row = selected_summary_by_index.get(idx_raw)
            if selected_row is None:
                continue
            row["pagina_inicial_pdf_ia"] = selected_row.get("pagina_inicial_pdf_ia", "")
            row["pagina_final_pdf_ia"] = selected_row.get("pagina_final_pdf_ia", "")
            row["aip_inicio"] = _format_aip_marker(row["pagina_inicial_pdf_ia"])
            row["aip_fim"] = _format_aip_marker(row["pagina_final_pdf_ia"])
            row["observacao_pdf_ia"] = selected_row.get("observacao", "")

        now_iso = datetime.now().isoformat(timespec="seconds")
        runtime_payload = {
            "versao": "1.0",
            "gerado_em": now_iso,
            "arquivo_zip": config.zip_path.name,
            "modo_processamento": "integral",
            "criterio_selecao_pecas_principais": "integral",
            "lacunas_existentes": False,
            "total_nao_fornecidas": 0,
            "total_folhas_processo": max_folio_in_process,
            "total_inconsistencias_paginas": len(inconsistency_rows),
            "total_itens": len(full_summary_rows),
            "itens": full_summary_rows,
        }
        write_json(integral_summary_runtime_path, runtime_payload)
        write_json(
            integral_summary_path,
            _build_public_full_summary_payload(
                now_iso=now_iso,
                zip_filename=config.zip_path.name,
                modo_processamento="integral",
                selection_criteria="integral",
                total_nao_fornecidas=0,
                max_folio_in_process=max_folio_in_process,
                inconsistency_rows=inconsistency_rows,
                full_summary_rows=full_summary_rows,
            ),
        )

        if ia_pdf_path:
            emit(msg_filter_full_pdf_generated(ia_pdf_inputs, ia_pdf_total_pages, ia_pdf_skipped))
        else:
            emit(msg_filter_full_pdf_not_generated())

        if inconsistency_rows:
            emit(f"Inconsistencias de paginacao identificadas: {len(inconsistency_rows)}.")

        emit("Processamento integral concluido.")
        return ProcessingResult(
            output_root=output_root,
            processing_mode="integral",
            integral_mode=True,
            total_files=total_files,
            main_files=main_files,
            auxiliary_files=auxiliary_files,
            ia_pdf_path=ia_pdf_path,
            ia_pdf_inputs=ia_pdf_inputs,
            ia_pdf_skipped=ia_pdf_skipped,
            ia_pdf_total_pages=ia_pdf_total_pages,
            selection_criteria="integral",
            selected_summary_json_path=None,
            selected_summary_md_path=None,
            full_summary_json_path=integral_summary_path,
            batch_output_root=None,
            batch_full_summary_json_path=None,
            batch_total=0,
        )

    if config.batch_only_mode:
        emit("Processamento de lotes IA (separado): preparando PDFs do ZIP...")
        if _normalize_batch_cut_mode(config.batch_cut_mode) == BATCH_CUT_MODE_TOKENS:
            emit(
                "Parametros de loteamento: "
                f"alvo ~{config.batch_target_tokens} tokens "
                f"(faixa {config.batch_min_tokens}-{config.batch_max_tokens})."
            )
        elif config.batch_max_size_mb > 0:
            emit(f"Parametros de loteamento: ~{config.batch_max_size_mb} MB maximos por lote.")
        else:
            emit("Parametros de loteamento: sem limite de MB (lote unico).")
        total_files = 0
        main_files = 0
        auxiliary_files = 0
        max_folio_in_process = 0
        main_pdf_inputs: list[PiecePdfInput] = []
        lotes_summary_fallback = output_root / "lotes_ia" / "arquivos_gerados" / FILTER_BATCH_FULL_SUMMARY_JSON_NAME

        with tempfile.TemporaryDirectory(prefix="filtro_lotes_ia_") as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)

            emit("Abrindo ZIP...")
            with zipfile.ZipFile(config.zip_path, "r") as zf:
                entries = [entry for entry in zf.infolist() if not entry.is_dir()]
                emit(f"{len(entries)} arquivos localizados no ZIP.")

                for idx, entry in enumerate(entries, start=1):
                    original_name = entry.filename
                    total_files += 1
                    folio_start, folio_end = extract_folio_range(original_name)
                    if folio_end is not None:
                        max_folio_in_process = max(max_folio_in_process, folio_end)

                    if Path(original_name).suffix.lower() != ".pdf":
                        auxiliary_files += 1
                        continue

                    category = base_category_from_name(original_name)
                    safe_file_name = sanitize_component(Path(original_name).name, "arquivo")
                    destination = unique_path(temp_dir / category / safe_file_name)
                    destination.parent.mkdir(parents=True, exist_ok=True)

                    with zf.open(entry, "r") as source:
                        with destination.open("wb") as target:
                            shutil.copyfileobj(source, target, length=1024 * 1024)

                    main_files += 1
                    main_pdf_inputs.append(
                        PiecePdfInput(
                            order_key=page_sort_key(original_name, idx),
                            source_index=idx,
                            original_name=original_name,
                            category=category,
                            pdf_path=destination,
                            folio_start=folio_start,
                            folio_end=folio_end,
                        )
                    )

                    if idx % 20 == 0 or idx == len(entries):
                        emit(f"Processados {idx}/{len(entries)} arquivos...")

            batch_output_root, batch_full_summary_json_path, batch_total = generate_ia_batches(
                pieces=main_pdf_inputs,
                selected_summary_rows=[],
                total_folios=max_folio_in_process,
                output_root=output_root,
                zip_filename=config.zip_path.name,
                selection_criteria="lotes_ia",
            batch_max_size_mb=config.batch_max_size_mb,
            batch_cut_mode=config.batch_cut_mode,
            batch_target_tokens=config.batch_target_tokens,
            batch_min_tokens=config.batch_min_tokens,
            batch_max_tokens=config.batch_max_tokens,
            replace_photo_pages=config.replace_photo_pages,
            crop_signature_strip_right=config.crop_signature_strip_right,
            crop_signature_strip_width_points=config.crop_signature_strip_width_points,
            emit=emit,
        )

        emit("Processamento de lotes IA concluido.")
        return ProcessingResult(
            output_root=output_root,
            processing_mode="lotes_ia",
            integral_mode=False,
            total_files=total_files,
            main_files=main_files,
            auxiliary_files=auxiliary_files,
            ia_pdf_path=None,
            ia_pdf_inputs=0,
            ia_pdf_skipped=0,
            ia_pdf_total_pages=0,
            selection_criteria="lotes_ia",
            selected_summary_json_path=None,
            selected_summary_md_path=None,
            full_summary_json_path=batch_full_summary_json_path or lotes_summary_fallback,
            batch_output_root=batch_output_root,
            batch_full_summary_json_path=batch_full_summary_json_path,
            batch_total=batch_total,
        )

    selected_main_categories = config.main_categories_override
    selected_main_file_indexes = config.main_file_indexes_override
    selected_main_folio_ranges = config.main_folio_ranges_override

    def selection_for_item(
        index: int,
        category: str,
        folio_start: int | None = None,
        folio_end: int | None = None,
    ) -> tuple[bool, str]:
        if config.integral_mode:
            return True, "processamento_integral"
        if selected_main_folio_ranges is not None:
            if piece_intersects_folio_ranges(folio_start, folio_end, selected_main_folio_ranges):
                return True, "selecao_folhas_exatas"
            return False, "nao_selecionado_folhas_exatas"
        if selected_main_file_indexes is not None:
            if index in selected_main_file_indexes:
                return True, "selecao_folhas"
            return False, "nao_selecionado_folhas"
        if selected_main_categories is not None:
            if category in selected_main_categories:
                return True, "selecao_usuario"
            return False, "nao_selecionado"
        group, rule = classify_master_folder(category, config.main_rules)
        return group == "pecas_principais", rule

    def parse_int(value: Any) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned.isdigit():
                return int(cleaned)
        return None

    if config.integral_mode:
        selection_criteria = "integral"
    elif selected_main_folio_ranges is not None:
        selection_criteria = "folhas_exatas"
    elif selected_main_file_indexes is not None:
        selection_criteria = "folhas"
    elif selected_main_categories is not None:
        selection_criteria = "categorias"
    else:
        selection_criteria = "regras"

    bootstrap_needed = (
        not full_summary_runtime_json_path.exists()
        or not main_root.exists()
        or not auxiliary_root.exists()
    )
    static_payload: dict[str, Any] | None = None
    if not bootstrap_needed:
        try:
            static_payload = json.loads(full_summary_runtime_json_path.read_text(encoding="utf-8-sig"))
        except Exception:
            bootstrap_needed = True
        else:
            static_rows = list(static_payload.get("itens", []))
            if not static_rows:
                bootstrap_needed = True
            else:
                missing_count = 0
                for row in static_rows:
                    rel_dest = str(row.get("destino", "")).strip()
                    if rel_dest and not (output_root / rel_dest).exists():
                        missing_count += 1
                        if missing_count >= 3:
                            break
                if missing_count >= 3:
                    emit("Base local inconsistente com o sumario completo. Reconstruindo estrutura base...")
                    bootstrap_needed = True

    total_files = 0
    main_files = 0
    auxiliary_files = 0
    full_summary_rows: list[dict[str, Any]] = []
    main_pdf_inputs: list[PiecePdfInput] = []
    max_folio_in_process = 0
    persist_static_summary = False

    if bootstrap_needed:
        emit("Primeira execucao detectada para este processo. Gerando estrutura base...")
        if main_root.exists():
            shutil.rmtree(main_root)
        if auxiliary_root.exists():
            shutil.rmtree(auxiliary_root)
        main_root.mkdir(parents=True, exist_ok=True)
        auxiliary_root.mkdir(parents=True, exist_ok=True)

        emit("Abrindo ZIP...")
        with zipfile.ZipFile(config.zip_path, "r") as zf:
            entries = [entry for entry in zf.infolist() if not entry.is_dir()]
            emit(f"{len(entries)} arquivos localizados no ZIP.")

            for idx, entry in enumerate(entries, start=1):
                original_name = entry.filename
                total_files += 1
                folio_start, folio_end = extract_folio_range(original_name)
                if folio_end is not None:
                    max_folio_in_process = max(max_folio_in_process, folio_end)

                category = base_category_from_name(original_name)
                safe_file_name = sanitize_component(Path(original_name).name, "arquivo")
                is_selected_now, matched_rule = selection_for_item(idx, category, folio_start, folio_end)
                master_group = "pecas_principais" if is_selected_now else "documentos_auxiliares"

                target_root = main_root if is_selected_now else auxiliary_root
                destination = unique_path(target_root / category / safe_file_name)
                destination.parent.mkdir(parents=True, exist_ok=True)

                with zf.open(entry, "r") as source:
                    with destination.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)

                if is_selected_now:
                    main_files += 1
                    if destination.suffix.lower() == ".pdf":
                        main_pdf_inputs.append(
                            PiecePdfInput(
                                order_key=page_sort_key(original_name, idx),
                                source_index=idx,
                                original_name=original_name,
                                category=category,
                                pdf_path=destination,
                                folio_start=folio_start,
                                folio_end=folio_end,
                            )
                        )
                else:
                    auxiliary_files += 1

                full_summary_rows.append(
                {
                    "indice": idx,
                    "arquivo_original": original_name,
                    "categoria": category,
                        "extensao": destination.suffix.lower(),
                        "fls_inicial_original": folio_start or "",
                        "fls_final_original": folio_end or "",
                        "status_fornecimento": "selecionada" if is_selected_now else "nao_selecionada",
                        "pasta_mestra": master_group,
                        "regra_classificacao": matched_rule,
                    "destino": str(destination.relative_to(output_root)),
                    "pagina_inicial_pdf_ia": "",
                    "pagina_final_pdf_ia": "",
                    "aip_inicio": _format_aip_marker(folio_start),
                    "aip_fim": _format_aip_marker(folio_end or folio_start),
                    "observacao_pdf_ia": "",
                }
            )

                if idx % 20 == 0 or idx == len(entries):
                    emit(f"Processados {idx}/{len(entries)} arquivos...")

        persist_static_summary = True
    else:
        emit("Estrutura base ja existente. Reutilizando pastas sem reorganizar.")
        payload = static_payload or json.loads(full_summary_runtime_json_path.read_text(encoding="utf-8-sig"))
        full_summary_rows = list(payload.get("itens", []))
        total_files = len(full_summary_rows)
        max_folio_in_process = parse_int(payload.get("total_folhas_processo")) or 0

        for row in full_summary_rows:
            idx = parse_int(row.get("indice"))
            if idx is None:
                continue
            category = str(row.get("categoria", ""))
            original_name = str(row.get("arquivo_original", ""))
            folio_start = parse_int(row.get("fls_inicial_original"))
            folio_end = parse_int(row.get("fls_final_original"))
            is_selected_now, _matched_rule = selection_for_item(idx, category, folio_start, folio_end)
            if folio_end is not None:
                max_folio_in_process = max(max_folio_in_process, folio_end)
            if is_selected_now:
                main_files += 1
            else:
                auxiliary_files += 1

            ext = str(row.get("extensao", "")).lower()
            if not (is_selected_now and ext == ".pdf"):
                continue

            rel_dest = str(row.get("destino", "")).strip()
            if not rel_dest:
                continue
            pdf_path = output_root / rel_dest
            if not pdf_path.exists():
                emit(f"Aviso: arquivo selecionado nao encontrado na base local: {rel_dest}")
                continue

            main_pdf_inputs.append(
                PiecePdfInput(
                    order_key=page_sort_key(original_name, idx),
                    source_index=idx,
                    original_name=original_name,
                    category=category,
                    pdf_path=pdf_path,
                    folio_start=folio_start,
                    folio_end=folio_end,
                )
            )

    # Define o proximo numero de execucao dos artefatos selecionados.
    execution_number = 1
    for existing in output_root.glob(FILTER_SELECTED_PDF_GLOB):
        existing_number = extract_filter_selected_execution_number(existing.name)
        if existing_number is None:
            continue
        execution_number = max(execution_number, existing_number + 1)

    emit("Gerando PDF anotado para IA ([AIP: X])...")
    ia_pdf_destination = output_root / build_filter_selected_pdf_name(execution_number)
    selected_summary_json_path: Path | None = None
    selected_summary_md_path: Path | None = None
    if not config.integral_mode:
        selected_summary_json_path = output_root / build_filter_selected_summary_json_name(execution_number)

    ia_pdf_path, ia_pdf_inputs, ia_pdf_skipped, ia_pdf_total_pages, selected_summary_rows, inconsistency_rows = build_ia_pdf_and_reports(
        main_pdf_inputs,
        max_folio_in_process,
        ia_pdf_destination,
        selected_summary_json_path,
        selected_summary_md_path,
        selected_folio_ranges=selected_main_folio_ranges,
        replace_photo_pages=config.replace_photo_pages,
        crop_signature_strip_right=config.crop_signature_strip_right,
        crop_signature_strip_width_points=config.crop_signature_strip_width_points,
    )
    if ia_pdf_path:
        emit(
            f"PDF para IA gerado com {ia_pdf_inputs} documentos, "
            f"{ia_pdf_total_pages} paginas (ignorados: {ia_pdf_skipped})."
        )
    else:
        emit("PDF para IA nao gerado (nenhum PDF principal valido).")

    selected_summary_by_index: dict[int, dict[str, str | int]] = {}
    for row in selected_summary_rows:
        idx_raw = row.get("ordem_compilacao")
        if isinstance(idx_raw, int):
            selected_summary_by_index[idx_raw] = row
            continue
        try:
            selected_summary_by_index[int(str(idx_raw))] = row
        except Exception:
            continue

    if persist_static_summary:
        if selected_main_folio_ranges is None:
            for row in full_summary_rows:
                idx = parse_int(row.get("indice"))
                if idx is None:
                    continue
                selected_row = selected_summary_by_index.get(idx)
                if selected_row is None:
                    continue
                row["pagina_inicial_pdf_ia"] = selected_row.get("pagina_inicial_pdf_ia", "")
                row["pagina_final_pdf_ia"] = selected_row.get("pagina_final_pdf_ia", "")
                row["aip_inicio"] = _format_aip_marker(row["pagina_inicial_pdf_ia"])
                row["aip_fim"] = _format_aip_marker(row["pagina_final_pdf_ia"])
                row["observacao_pdf_ia"] = selected_row.get("observacao", "")

        now_iso = datetime.now().isoformat(timespec="seconds")
        total_nao_fornecidas = sum(1 for row in full_summary_rows if row.get("status_fornecimento") == "nao_selecionada")
        runtime_payload = {
            "versao": "1.0",
            "gerado_em": now_iso,
            "arquivo_zip": config.zip_path.name,
            "modo_processamento": "integral" if config.integral_mode else "seletivo",
            "criterio_selecao_pecas_principais": selection_criteria,
            "lacunas_existentes": total_nao_fornecidas > 0,
            "total_nao_fornecidas": total_nao_fornecidas,
            "total_folhas_processo": max_folio_in_process,
            "total_inconsistencias_paginas": len(inconsistency_rows),
            "total_itens": len(full_summary_rows),
            "itens": full_summary_rows,
        }
        write_json(full_summary_runtime_json_path, runtime_payload)
        write_json(
            full_summary_json_path,
            _build_public_full_summary_payload(
                now_iso=now_iso,
                zip_filename=config.zip_path.name,
                modo_processamento="integral" if config.integral_mode else "seletivo",
                selection_criteria=selection_criteria,
                total_nao_fornecidas=total_nao_fornecidas,
                max_folio_in_process=max_folio_in_process,
                inconsistency_rows=inconsistency_rows,
                full_summary_rows=full_summary_rows,
            ),
        )
        emit("Sumario completo consolidado na primeira execucao.")
    else:
        emit("Sumario completo preservado (sem nova geracao).")

    if inconsistency_rows:
        emit(f"Inconsistencias de paginacao identificadas: {len(inconsistency_rows)}.")

    emit("Processamento concluido.")

    return ProcessingResult(
        output_root=output_root,
        processing_mode="seletivo",
        integral_mode=config.integral_mode,
        total_files=total_files,
        main_files=main_files,
        auxiliary_files=auxiliary_files,
        ia_pdf_path=ia_pdf_path,
        ia_pdf_inputs=ia_pdf_inputs,
        ia_pdf_skipped=ia_pdf_skipped,
        ia_pdf_total_pages=ia_pdf_total_pages,
        selection_criteria=selection_criteria,
        selected_summary_json_path=selected_summary_json_path,
        selected_summary_md_path=selected_summary_md_path,
        full_summary_json_path=full_summary_json_path,
        batch_output_root=None,
        batch_full_summary_json_path=None,
        batch_total=0,
    )


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Organizador de Processo Judicial")
        self.geometry("980x680")

        self.zip_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(default_output_dir()))
        self.batch_max_size_mb_var = tk.StringVar(value=str(DEFAULT_BATCH_MAX_SIZE_MB))
        self.replace_photo_pages_var = tk.BooleanVar(value=True)
        self.crop_signature_strip_right_var = tk.BooleanVar(value=False)
        self.crop_signature_strip_width_var = tk.StringVar(value=f"{DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS:.1f}")
        self.rules_info_var = tk.StringVar()
        self.selection_info_var = tk.StringVar(value="Selecao seletiva: nao definida")

        self.worker_queue: queue.Queue[tuple[str, str | ProcessingResult]] = queue.Queue()
        self.processing_thread: threading.Thread | None = None
        self.rules_editor: tk.Toplevel | None = None
        self.selection_editor: tk.Toplevel | None = None
        self.current_category_counts: dict[str, int] = {}
        self.current_piece_candidates: list[dict[str, Any]] = []
        self.selected_main_categories: set[str] = set()
        self.selected_main_file_indexes: set[int] = set()
        self.selection_mode: str | None = None
        self.selection_zip_path: Path | None = None
        self.last_output_root: Path | None = None
        self.result_tree_paths: dict[str, Path] = {}

        self.startup_warning: str | None = None
        try:
            self.main_rules = load_rules(RULES_FILE)
        except Exception as exc:
            self.main_rules = validate_rules(DEFAULT_RULES)
            save_rules(RULES_FILE, self.main_rules)
            self.startup_warning = f"Nao foi possivel ler o arquivo de regras. Padrao carregado.\n\nDetalhe: {exc}"

        self._build_ui()
        self._refresh_rules_info()
        self._refresh_selection_info()
        self.after(200, self._poll_worker_queue)
        if self.startup_warning:
            self.after(400, lambda: messagebox.showwarning("Regras", self.startup_warning))

    def _bind_mousewheel_to_canvas(self, canvas: tk.Canvas, widgets: list[tk.Misc]) -> None:
        def on_mousewheel(event: tk.Event) -> str | None:
            delta = getattr(event, "delta", 0)
            if delta:
                steps = -int(delta / 120)
                if steps == 0:
                    steps = -1 if delta > 0 else 1
            else:
                num = getattr(event, "num", None)
                if num == 4:
                    steps = -1
                elif num == 5:
                    steps = 1
                else:
                    return None
            canvas.yview_scroll(steps, "units")
            return "break"

        for widget in widgets:
            widget.bind("<MouseWheel>", on_mousewheel, add="+")
            widget.bind("<Button-4>", on_mousewheel, add="+")
            widget.bind("<Button-5>", on_mousewheel, add="+")

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self, padding=12)
        root_frame.pack(fill="both", expand=True)

        paned = ttk.Panedwindow(root_frame, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned, padding=(8, 0, 0, 0))
        paned.add(left, weight=3)
        paned.add(right, weight=2)

        ttk.Label(left, text="Arquivo ZIP do processo").grid(row=0, column=0, sticky="w")
        ttk.Entry(left, textvariable=self.zip_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(left, text="Selecionar ZIP", command=self.select_zip).grid(row=1, column=1)

        ttk.Label(left, text="Pasta de saida").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(left, textvariable=self.output_var).grid(row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(left, text="Selecionar pasta", command=self.select_output).grid(row=3, column=1)

        ttk.Label(
            left,
            text=(
                "Pecas principais: decisoes, despachos, peticoes, manifestacoes/pareceres do MP, "
                "contestacao e laudos periciais."
            ),
            wraplength=620,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 2))
        ttk.Label(
            left,
            text="Demais tipos vao para documentos auxiliares (selecao por categorias ou por folhas).",
            wraplength=620,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        buttons = ttk.Frame(left)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        buttons.columnconfigure(2, weight=1)
        buttons.columnconfigure(3, weight=1)

        self.run_category_button = ttk.Button(buttons, text="Processar por categorias", command=self.start_category_processing)
        self.run_category_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.run_folios_button = ttk.Button(buttons, text="Processar por folhas", command=self.start_folio_processing)
        self.run_folios_button.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.run_integral_button = ttk.Button(
            buttons,
            text="Processamento integral",
            command=self.start_integral_processing,
        )
        self.run_integral_button.grid(row=0, column=2, sticky="ew")
        self.run_batch_only_button = ttk.Button(
            buttons,
            text="Gerar lotes IA",
            command=self.start_batch_only_processing,
        )
        self.run_batch_only_button.grid(row=0, column=3, sticky="ew")

        ttk.Label(buttons, text="Max MB/lote (0=sem limite):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(buttons, textvariable=self.batch_max_size_mb_var, width=10).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(
            buttons,
            text="Substituir fotografias por pagina marcador (mantem paginacao)",
            variable=self.replace_photo_pages_var,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            buttons,
            text="Recortar tarja de assinatura na lateral direita",
            variable=self.crop_signature_strip_right_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(buttons, text="Largura corte (pt):").grid(row=3, column=2, sticky="e", pady=(4, 0))
        ttk.Entry(buttons, textvariable=self.crop_signature_strip_width_var, width=8).grid(
            row=3, column=3, sticky="e", pady=(4, 0)
        )
        ttk.Label(buttons, text="Preset corte (pt):").grid(row=4, column=2, sticky="e", pady=(4, 0))
        crop_preset_values = [f"{value:.0f}" for value in SIGNATURE_STRIP_PRESETS_PT]
        crop_preset_box = ttk.Combobox(buttons, values=crop_preset_values, width=6, state="readonly")
        crop_preset_box.grid(row=4, column=3, sticky="e", pady=(4, 0))
        try:
            current_width = float(str(self.crop_signature_strip_width_var.get()).strip().replace(",", "."))
        except Exception:
            current_width = DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS
        preset_default = min(SIGNATURE_STRIP_PRESETS_PT, key=lambda value: abs(value - current_width))
        crop_preset_box.set(f"{preset_default:.0f}")

        def _on_crop_preset_selected(_event: object | None = None) -> None:
            selected = str(crop_preset_box.get()).strip()
            if not selected:
                return
            self.crop_signature_strip_width_var.set(selected)

        crop_preset_box.bind("<<ComboboxSelected>>", _on_crop_preset_selected)

        ttk.Button(buttons, text="Limpar log", command=self.clear_log).grid(row=5, column=3, sticky="e", pady=(6, 0))

        status_row = ttk.Frame(left)
        status_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        status_row.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Pronto")
        ttk.Label(status_row, textvariable=self.status_var).grid(row=0, column=0, sticky="e")

        ttk.Label(left, textvariable=self.rules_info_var).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(left, textvariable=self.selection_info_var).grid(row=9, column=0, columnspan=3, sticky="w")

        ttk.Label(left, text="Log de execucao").grid(row=10, column=0, sticky="w")

        log_frame = ttk.Frame(left)
        log_frame.grid(row=11, column=0, columnspan=3, sticky="nsew")

        self.log_text = tk.Text(log_frame, height=22, wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)

        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(11, weight=1)

        ttk.Label(right, text="Visualizacao dos arquivos gerados").grid(row=0, column=0, sticky="w")

        right_actions = ttk.Frame(right)
        right_actions.grid(row=1, column=0, sticky="w", pady=(6, 6))
        ttk.Button(right_actions, text="Atualizar lista", command=self.refresh_results_view).grid(row=0, column=0)
        ttk.Button(right_actions, text="Abrir pasta de saída", command=self.open_output_folder).grid(
            row=0, column=1, padx=(8, 0)
        )

        tree_frame = ttk.Frame(right)
        tree_frame.grid(row=2, column=0, sticky="nsew")

        self.results_tree = ttk.Treeview(tree_frame, columns=("tipo", "tamanho"), show="tree headings", selectmode="browse")
        self.results_tree.heading("#0", text="Nome")
        self.results_tree.heading("tipo", text="Tipo")
        self.results_tree.heading("tamanho", text="Tamanho")
        self.results_tree.column("#0", width=300, anchor="w")
        self.results_tree.column("tipo", width=90, anchor="center")
        self.results_tree.column("tamanho", width=110, anchor="e")

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.results_tree.yview)
        tree_scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        self.results_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")

        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.results_tree.bind("<<TreeviewOpen>>", self._on_results_tree_open)
        self.results_tree.bind("<Double-1>", self.open_selected_result)

        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        self.refresh_results_view()

    def _format_file_size(self, size_bytes: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        size = float(size_bytes)
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1
        if unit_index == 0:
            return f"{int(size)} {units[unit_index]}"
        return f"{size:.2f} {units[unit_index]}"

    def _clear_results_tree(self) -> None:
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.result_tree_paths.clear()

    def _insert_result_node(self, parent_id: str, path: Path) -> None:
        if path.is_dir():
            node_id = self.results_tree.insert(parent_id, "end", text=path.name, values=("pasta", ""))
            self.result_tree_paths[node_id] = path
            # Placeholder para carregamento sob demanda quando a pasta for expandida.
            dummy_id = self.results_tree.insert(node_id, "end", text="__loading__", values=("", ""))
            self.result_tree_paths[dummy_id] = path
            return

        size_text = ""
        try:
            size_text = self._format_file_size(path.stat().st_size)
        except Exception:
            size_text = ""
        node_id = self.results_tree.insert(parent_id, "end", text=path.name, values=("arquivo", size_text))
        self.result_tree_paths[node_id] = path

    def _populate_tree_children(self, parent_id: str, parent_path: Path) -> None:
        for child_id in self.results_tree.get_children(parent_id):
            self.result_tree_paths.pop(child_id, None)
            self.results_tree.delete(child_id)

        try:
            children = sorted(parent_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception:
            children = []

        for child in children:
            self._insert_result_node(parent_id, child)

    def _on_results_tree_open(self, _event: object | None = None) -> None:
        node_id = self.results_tree.focus()
        if not node_id:
            return

        node_path = self.result_tree_paths.get(node_id)
        if not node_path or not node_path.is_dir():
            return

        children = self.results_tree.get_children(node_id)
        if len(children) != 1:
            return

        child_text = self.results_tree.item(children[0], "text")
        if child_text != "__loading__":
            return

        self._populate_tree_children(node_id, node_path)

    def refresh_results_view(self, target: Path | None = None) -> None:
        if target is None:
            if self.last_output_root and self.last_output_root.exists():
                target = self.last_output_root
            else:
                base = parse_output_dir(self.output_var.get())
                target = base if base.exists() else None

        self._clear_results_tree()

        if target is None or not target.exists():
            return

        root_id = self.results_tree.insert("", "end", text=str(target), values=("pasta raiz", ""))
        self.result_tree_paths[root_id] = target

        if target.is_dir():
            self._populate_tree_children(root_id, target)

        self.results_tree.item(root_id, open=True)
        self.results_tree.selection_set(root_id)
        self.results_tree.focus(root_id)

    def open_selected_result(self, _event: object | None = None) -> None:
        selected = self.results_tree.selection()
        if not selected:
            messagebox.showinfo("Abrir", "Selecione um item na lista de resultados.")
            return

        item_id = selected[0]
        path = self.result_tree_paths.get(item_id)
        if not path or not path.exists():
            messagebox.showerror("Erro", "Item selecionado nao esta mais disponivel.")
            return

        try:
            os.startfile(str(path))
            self.append_log(f"Item aberto: {path}")
        except Exception as exc:
            messagebox.showerror("Erro", f"Nao foi possivel abrir o item: {exc}")

    def _refresh_rules_info(self) -> None:
        self.rules_info_var.set(f"Regras ativas: {len(self.main_rules)} | Arquivo: {RULES_FILE.name}")

    def _refresh_selection_info(self) -> None:
        if not self.selection_zip_path:
            self.selection_info_var.set("Selecao seletiva: nao definida")
            return

        if self.selection_mode == "folhas":
            total_files = len(self.current_piece_candidates)
            selected_files = len(self.selected_main_file_indexes)
            self.selection_info_var.set(
                "Selecao ativa (folhas): "
                f"{selected_files}/{total_files} pecas em pecas principais"
            )
            return

        total_categories = len(self.current_category_counts)
        selected_categories = len(self.selected_main_categories)
        selected_files = sum(self.current_category_counts.get(category, 0) for category in self.selected_main_categories)
        self.selection_info_var.set(
            "Selecao ativa (categorias): "
            f"{selected_categories}/{total_categories} categorias em pecas principais "
            f"({selected_files} arquivos)"
        )

    def _zip_path_is_current(self, zip_path: Path) -> bool:
        if self.selection_zip_path is None:
            return False
        return self.selection_zip_path == zip_path.resolve(strict=False)

    def preprocess_categories(self) -> bool:
        zip_path = Path(self.zip_var.get().strip())

        if not zip_path.exists():
            messagebox.showerror("Erro", "Selecione um arquivo ZIP valido.")
            return False

        if zip_path.suffix.lower() != ".zip":
            messagebox.showerror("Erro", "O arquivo selecionado precisa ter extensao .zip.")
            return False

        try:
            category_counts = collect_zip_categories(zip_path)
        except Exception as exc:
            messagebox.showerror("Erro", f"Nao foi possivel ler o ZIP: {exc}")
            return False

        if not category_counts:
            messagebox.showerror("Erro", "Nenhum arquivo foi encontrado dentro do ZIP.")
            return False

        suggested_categories = {
            category
            for category in category_counts
            if classify_master_folder(category, self.main_rules)[0] == "pecas_principais"
        }

        if self._zip_path_is_current(zip_path):
            base_selection = {category for category in self.selected_main_categories if category in category_counts}
            base_selection.update(category for category in suggested_categories if category not in self.current_category_counts)
        else:
            base_selection = set(suggested_categories)

        selected = self._open_category_selector_dialog(zip_path, category_counts, base_selection, suggested_categories)
        if selected is None:
            return False

        self.current_category_counts = category_counts
        self.selected_main_categories = selected
        self.current_piece_candidates = []
        self.selected_main_file_indexes = set()
        self.selection_mode = "categorias"
        self.selection_zip_path = zip_path.resolve(strict=False)
        self._refresh_selection_info()

        selected_files = sum(category_counts.get(category, 0) for category in selected)
        self.append_log(
            f"Pre-processamento concluido: {len(category_counts)} categorias | "
            f"{len(selected)} categorias selecionadas ({selected_files} arquivos)."
        )
        return True

    def preprocess_folios(self) -> bool:
        zip_path = Path(self.zip_var.get().strip())

        if not zip_path.exists():
            messagebox.showerror("Erro", "Selecione um arquivo ZIP valido.")
            return False

        if zip_path.suffix.lower() != ".zip":
            messagebox.showerror("Erro", "O arquivo selecionado precisa ter extensao .zip.")
            return False

        try:
            piece_candidates = collect_zip_piece_candidates(zip_path, self.main_rules)
        except Exception as exc:
            messagebox.showerror("Erro", f"Nao foi possivel ler o ZIP: {exc}")
            return False

        if not piece_candidates:
            messagebox.showerror("Erro", "Nenhum arquivo foi encontrado dentro do ZIP.")
            return False

        suggested_indexes = {
            int(piece["source_index"])
            for piece in piece_candidates
            if bool(piece.get("sugerida"))
        }
        valid_indexes = {int(piece["source_index"]) for piece in piece_candidates}

        if self._zip_path_is_current(zip_path) and self.selection_mode == "folhas":
            base_selection = {idx for idx in self.selected_main_file_indexes if idx in valid_indexes}
            if not base_selection:
                base_selection = set(suggested_indexes)
        else:
            base_selection = set(suggested_indexes)

        selected = self._open_piece_selector_dialog(zip_path, piece_candidates, base_selection)
        if selected is None:
            return False

        self.current_piece_candidates = piece_candidates
        self.selected_main_file_indexes = selected
        self.current_category_counts = {}
        self.selected_main_categories = set()
        self.selection_mode = "folhas"
        self.selection_zip_path = zip_path.resolve(strict=False)
        self._refresh_selection_info()

        selected_with_folio = sum(
            1
            for piece in piece_candidates
            if int(piece["source_index"]) in selected and piece.get("fls_inicial_original") is not None
        )
        self.append_log(
            "Pre-processamento por folhas concluido: "
            f"{len(piece_candidates)} pecas listadas | "
            f"{len(selected)} selecionadas ({selected_with_folio} com faixa de folhas identificada)."
        )
        return True

    def _open_category_selector_dialog(
        self,
        zip_path: Path,
        category_counts: dict[str, int],
        initial_selection: set[str],
        suggested_categories: set[str],
    ) -> set[str] | None:
        if self.selection_editor and self.selection_editor.winfo_exists():
            self.selection_editor.destroy()

        dialog = tk.Toplevel(self)
        dialog.title(f"Selecionar categorias principais - {zip_path.name}")
        dialog.geometry("900x640")
        dialog.transient(self)
        dialog.grab_set()
        self.selection_editor = dialog

        container = ttk.Frame(dialog, padding=10)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=(
                "Selecione as categorias que devem ir para 01_pecas_principais. "
                "As demais irao para 02_documentos_auxiliares."
            ),
        ).pack(anchor="w")

        search_var = tk.StringVar()
        summary_var = tk.StringVar()
        visible_var = tk.StringVar()

        search_row = ttk.Frame(container)
        search_row.pack(fill="x", pady=(8, 6))
        ttk.Label(search_row, text="Filtrar categorias:").pack(side="left")
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        scroller = ttk.Frame(container)
        scroller.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroller, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(scroller, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        checks_frame = ttk.Frame(canvas)
        frame_window = canvas.create_window((0, 0), window=checks_frame, anchor="nw")

        checks_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(frame_window, width=event.width))
        scroll_widgets: list[tk.Misc] = [canvas, checks_frame, search_entry]

        sorted_categories = sorted(
            category_counts.items(),
            key=lambda item: (-item[1], normalize_for_match(item[0])),
        )

        check_vars: dict[str, tk.BooleanVar] = {}
        check_widgets: dict[str, ttk.Checkbutton] = {}

        def update_summary() -> None:
            selected_categories = [category for category, var in check_vars.items() if var.get()]
            selected_files = sum(category_counts.get(category, 0) for category in selected_categories)
            summary_var.set(
                f"Selecionadas: {len(selected_categories)} categorias | "
                f"Arquivos nessas categorias: {selected_files}"
            )

        for category, count in sorted_categories:
            var = tk.BooleanVar(value=category in initial_selection)
            check_vars[category] = var
            cb = ttk.Checkbutton(
                checks_frame,
                text=f"{category} ({count})",
                variable=var,
                command=update_summary,
            )
            check_widgets[category] = cb
            cb.pack(fill="x", anchor="w")
            scroll_widgets.append(cb)

        self._bind_mousewheel_to_canvas(canvas, scroll_widgets)

        def refresh_filter(*_args: object) -> None:
            needle = normalize_for_match(search_var.get())
            shown = 0
            for category, _count in sorted_categories:
                widget = check_widgets[category]
                hay = normalize_for_match(category)
                if needle and needle not in hay:
                    widget.pack_forget()
                    continue
                widget.pack(fill="x", anchor="w")
                shown += 1
            visible_var.set(f"Exibidas: {shown}/{len(sorted_categories)} categorias")

        search_var.trace_add("write", refresh_filter)
        refresh_filter()
        update_summary()

        status_row = ttk.Frame(container)
        status_row.pack(fill="x", pady=(6, 6))
        ttk.Label(status_row, textvariable=summary_var).pack(side="left")
        ttk.Label(status_row, textvariable=visible_var).pack(side="right")

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(4, 0))

        def mark_suggested() -> None:
            for category, var in check_vars.items():
                var.set(category in suggested_categories)
            update_summary()

        def mark_all() -> None:
            for var in check_vars.values():
                var.set(True)
            update_summary()

        def clear_all() -> None:
            for var in check_vars.values():
                var.set(False)
            update_summary()

        ttk.Button(actions, text="Marcar sugeridas", command=mark_suggested).pack(side="left")
        ttk.Button(actions, text="Marcar todas", command=mark_all).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Limpar", command=clear_all).pack(side="left", padx=(8, 0))

        result: set[str] | None = None

        def confirm_selection() -> None:
            nonlocal result
            result = {category for category, var in check_vars.items() if var.get()}
            dialog.destroy()

        def cancel_selection() -> None:
            nonlocal result
            result = None
            dialog.destroy()

        ttk.Button(actions, text="Cancelar", command=cancel_selection).pack(side="right")
        ttk.Button(actions, text="Processar com selecao", command=confirm_selection).pack(side="right", padx=(0, 8))

        dialog.protocol("WM_DELETE_WINDOW", cancel_selection)
        dialog.wait_visibility()
        search_entry.focus_set()
        self.wait_window(dialog)
        self.selection_editor = None
        return result

    def _open_piece_selector_dialog(
        self,
        zip_path: Path,
        piece_candidates: list[dict[str, Any]],
        initial_selection: set[int],
    ) -> set[int] | None:
        if self.selection_editor and self.selection_editor.winfo_exists():
            self.selection_editor.destroy()

        dialog = tk.Toplevel(self)
        dialog.title(f"Selecionar pecas por folhas - {zip_path.name}")
        dialog.geometry("1100x700")
        dialog.transient(self)
        dialog.grab_set()
        self.selection_editor = dialog

        container = ttk.Frame(dialog, padding=10)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=(
                "Selecione as pecas principais pela numeracao de folhas (ordenadas por fls). "
                "As nao selecionadas irao para 02_documentos_auxiliares."
            ),
        ).pack(anchor="w")

        search_var = tk.StringVar()
        folio_range_var = tk.StringVar()
        summary_var = tk.StringVar()
        visible_var = tk.StringVar()

        search_row = ttk.Frame(container)
        search_row.pack(fill="x", pady=(8, 6))
        ttk.Label(search_row, text="Filtrar lista:").pack(side="left")
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        folio_row = ttk.Frame(container)
        folio_row.pack(fill="x", pady=(0, 6))
        ttk.Label(folio_row, text="Selecionar por faixa de folhas:").pack(side="left")
        folio_entry = ttk.Entry(folio_row, textvariable=folio_range_var, width=36)
        folio_entry.pack(side="left", padx=(8, 0))
        ttk.Label(folio_row, text="Ex.: 120-180, 240, 300-320").pack(side="left", padx=(8, 0))

        scroller = ttk.Frame(container)
        scroller.pack(fill="both", expand=True)

        canvas = tk.Canvas(scroller, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(scroller, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        checks_frame = ttk.Frame(canvas)
        frame_window = canvas.create_window((0, 0), window=checks_frame, anchor="nw")

        checks_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(frame_window, width=event.width))
        scroll_widgets: list[tk.Misc] = [canvas, checks_frame, search_entry, folio_entry]

        check_vars: dict[int, tk.BooleanVar] = {}
        check_widgets: dict[int, ttk.Checkbutton] = {}
        search_tokens: dict[int, str] = {}
        style = ttk.Style(dialog)
        base_font = tkfont.nametofont("TkDefaultFont")
        suggested_font = base_font.copy()
        suggested_font.configure(weight="bold")
        row_style = "FolioRow.TCheckbutton"
        suggested_row_style = "FolioSuggestedRow.TCheckbutton"
        style.configure(row_style, font=base_font)
        style.configure(suggested_row_style, font=suggested_font)

        def format_folio_text(piece: dict[str, Any]) -> str:
            start = piece.get("fls_inicial_original")
            end = piece.get("fls_final_original")
            if isinstance(start, int) and isinstance(end, int):
                return f"{start}-{end}" if start != end else str(start)
            return "NA"

        def format_piece_title(piece: dict[str, Any]) -> str:
            category = str(piece.get("categoria", "")).strip()
            if category:
                parts = [part.strip() for part in category.split("|") if part.strip()]
                if parts:
                    return parts[-1]

            original = str(piece.get("arquivo_original", "")).strip()
            if original:
                stem = PAGE_SUFFIX_RE.sub("", Path(original).stem).strip()
                if stem:
                    return stem
            return "Documento"

        def build_piece_label(piece: dict[str, Any]) -> str:
            folio_text = format_folio_text(piece)
            title = format_piece_title(piece)
            return f"fls. {folio_text} - {title}"

        def update_summary() -> None:
            selected_indexes = {idx for idx, var in check_vars.items() if var.get()}
            selected_with_folio = sum(
                1
                for piece in piece_candidates
                if int(piece["source_index"]) in selected_indexes and piece.get("fls_inicial_original") is not None
            )
            summary_var.set(
                f"Selecionadas: {len(selected_indexes)} pecas | "
                f"Com faixa de folhas: {selected_with_folio}"
            )

        for piece in piece_candidates:
            idx = int(piece["source_index"])
            label = build_piece_label(piece)
            token = " ".join(
                [
                    format_folio_text(piece),
                    format_piece_title(piece),
                    str(piece.get("categoria", "")),
                    str(piece.get("arquivo_original", "")),
                ]
            )
            search_tokens[idx] = normalize_for_match(token)
            var = tk.BooleanVar(value=idx in initial_selection)
            check_vars[idx] = var
            cb = ttk.Checkbutton(
                checks_frame,
                text=label,
                variable=var,
                command=update_summary,
                style=suggested_row_style if bool(piece.get("sugerida")) else row_style,
            )
            check_widgets[idx] = cb
            cb.pack(fill="x", anchor="w")
            scroll_widgets.append(cb)

        self._bind_mousewheel_to_canvas(canvas, scroll_widgets)

        def refresh_filter(*_args: object) -> None:
            needle = normalize_for_match(search_var.get())
            shown = 0
            for piece in piece_candidates:
                idx = int(piece["source_index"])
                widget = check_widgets[idx]
                if needle and needle not in search_tokens[idx]:
                    widget.pack_forget()
                    continue
                widget.pack(fill="x", anchor="w")
                shown += 1
            visible_var.set(f"Exibidas: {shown}/{len(piece_candidates)} pecas")

        initial_selected = set(initial_selection)

        def get_selected_indexes() -> set[int]:
            return {idx for idx, var in check_vars.items() if var.get()}

        def apply_folio_ranges() -> bool:
            raw = folio_range_var.get().strip()
            if not raw:
                messagebox.showinfo("Faixas de folhas", "Informe ao menos uma faixa (ex.: 120-180, 240).")
                return False
            try:
                ranges = parse_folio_selection_ranges(raw)
            except ValueError as exc:
                messagebox.showerror("Faixas de folhas", str(exc))
                return False

            matched_indexes: set[int] = set()
            for piece in piece_candidates:
                idx = int(piece["source_index"])
                start = piece.get("fls_inicial_original")
                end = piece.get("fls_final_original")
                if piece_intersects_folio_ranges(
                    start if isinstance(start, int) else None,
                    end if isinstance(end, int) else None,
                    ranges,
                ):
                    matched_indexes.add(idx)

            if not matched_indexes:
                messagebox.showinfo("Faixas de folhas", "Nenhuma peca com faixa de folhas compativel foi encontrada.")
                return False

            for idx, var in check_vars.items():
                var.set(idx in matched_indexes)
            update_summary()
            return True

        search_var.trace_add("write", refresh_filter)
        refresh_filter()
        update_summary()

        status_row = ttk.Frame(container)
        status_row.pack(fill="x", pady=(6, 6))
        ttk.Label(status_row, textvariable=summary_var).pack(side="left")
        ttk.Label(status_row, textvariable=visible_var).pack(side="right")

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(4, 0))

        def mark_suggested() -> None:
            for piece in piece_candidates:
                idx = int(piece["source_index"])
                check_vars[idx].set(bool(piece.get("sugerida")))
            update_summary()

        def mark_all() -> None:
            for var in check_vars.values():
                var.set(True)
            update_summary()

        def clear_all() -> None:
            for var in check_vars.values():
                var.set(False)
            update_summary()

        ttk.Button(actions, text="Marcar sugeridas", command=mark_suggested).pack(side="left")
        ttk.Button(actions, text="Marcar todas", command=mark_all).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Limpar", command=clear_all).pack(side="left", padx=(8, 0))
        result: set[int] | None = None

        def finalize_selection(selected: set[int]) -> bool:
            nonlocal result
            if not selected:
                proceed = messagebox.askyesno(
                    "Selecao vazia",
                    "Nenhuma peca foi selecionada como principal. Deseja continuar assim mesmo?",
                )
                if not proceed:
                    return False
            result = selected
            dialog.destroy()
            return True

        def confirm_selection() -> None:
            finalize_selection(get_selected_indexes())

        def apply_ranges_and_confirm() -> None:
            if not apply_folio_ranges():
                return
            finalize_selection(get_selected_indexes())

        def cancel_selection() -> None:
            nonlocal result
            current_selection = get_selected_indexes()
            if current_selection != initial_selected:
                save_before_close = messagebox.askyesnocancel(
                    "Salvar selecao",
                    "A selecao foi alterada. Deseja salvar antes de fechar?",
                )
                if save_before_close is None:
                    return
                if save_before_close:
                    if not finalize_selection(current_selection):
                        return
                    return
            result = None
            dialog.destroy()

        ttk.Button(folio_row, text="Selecionar faixa e fechar", command=apply_ranges_and_confirm).pack(side="left", padx=(16, 0))
        ttk.Button(actions, text="Salvar selecao e fechar", command=confirm_selection).pack(side="left", padx=(16, 0))
        ttk.Button(actions, text="Cancelar", command=cancel_selection).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", cancel_selection)
        dialog.wait_visibility()
        search_entry.focus_set()
        self.wait_window(dialog)
        self.selection_editor = None
        return result

    def _rules_payload_text(self, rules: list[dict[str, Any]]) -> str:
        return json.dumps({"rules": rules}, ensure_ascii=False, indent=2)

    def open_rules_editor(self) -> None:
        if self.rules_editor and self.rules_editor.winfo_exists():
            self.rules_editor.lift()
            self.rules_editor.focus_force()
            return

        editor = tk.Toplevel(self)
        editor.title("Editar regras de pecas principais")
        editor.geometry("900x620")
        editor.transient(self)
        self.rules_editor = editor

        container = ttk.Frame(editor, padding=10)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=(
                "Edite o JSON abaixo e salve. Cada regra precisa de: id, label, all_terms, any_terms.\n"
                "all_terms: todos os termos devem aparecer. any_terms: pelo menos um deve aparecer."
            ),
        ).pack(anchor="w")

        text_frame = ttk.Frame(container)
        text_frame.pack(fill="both", expand=True, pady=(8, 8))

        text = tk.Text(text_frame, wrap="none")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", self._rules_payload_text(self.main_rules))

        scroll_y = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        scroll_y.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll_y.set)

        actions = ttk.Frame(container)
        actions.pack(fill="x")

        def restore_default() -> None:
            if not messagebox.askyesno("Restaurar", "Substituir por regras padrao?"):
                return
            text.delete("1.0", "end")
            text.insert("1.0", self._rules_payload_text(validate_rules(DEFAULT_RULES)))

        def save_from_editor() -> None:
            raw_text = text.get("1.0", "end").strip()
            if not raw_text:
                messagebox.showerror("Erro", "O conteudo nao pode ficar vazio.")
                return

            try:
                payload = json.loads(raw_text)
                new_rules = parse_rules_payload(payload)
            except Exception as exc:
                messagebox.showerror("Erro de validacao", str(exc))
                return

            try:
                save_rules(RULES_FILE, new_rules)
            except Exception as exc:
                messagebox.showerror("Erro", f"Nao foi possivel salvar: {exc}")
                return

            self.main_rules = new_rules
            self._refresh_rules_info()
            self.append_log(f"Regras atualizadas. Total: {len(new_rules)}")
            messagebox.showinfo("Regras", "Regras salvas com sucesso.")

        ttk.Button(actions, text="Restaurar padrao", command=restore_default).pack(side="left")
        ttk.Button(actions, text="Salvar regras", command=save_from_editor).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Fechar", command=editor.destroy).pack(side="right")

        editor.protocol("WM_DELETE_WINDOW", editor.destroy)

    def select_zip(self) -> None:
        path = filedialog.askopenfilename(
            title="Selecione o ZIP do processo",
            filetypes=[("Arquivos ZIP", "*.zip"), ("Todos os arquivos", "*.*")],
        )
        if path:
            new_zip = Path(path).resolve(strict=False)
            if self.selection_zip_path and self.selection_zip_path != new_zip:
                self.current_category_counts = {}
                self.current_piece_candidates = []
                self.selected_main_categories = set()
                self.selected_main_file_indexes = set()
                self.selection_mode = None
                self.selection_zip_path = None
                self._refresh_selection_info()
            self.zip_var.set(path)

    def select_output(self) -> None:
        path = filedialog.askdirectory(title="Selecione a pasta de saida")
        if path:
            self.output_var.set(path)
            self.refresh_results_view(Path(path))

    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")

    def clear_log(self) -> None:
        self.log_text.delete("1.0", "end")
        self.append_log("Log limpo.")

    def open_output_folder(self) -> None:
        target: Path | None = None

        if self.last_output_root and self.last_output_root.exists():
            target = self.last_output_root
        else:
            output_dir = parse_output_dir(self.output_var.get())
            if output_dir.exists():
                target = output_dir

        if target is None:
            messagebox.showerror("Erro", "Nenhuma pasta de saida disponivel para abrir.")
            return

        try:
            os.startfile(str(target))
            self.append_log(f"Pasta aberta: {target}")
            self.refresh_results_view(target)
        except Exception as exc:
            messagebox.showerror("Erro", f"Nao foi possivel abrir a pasta: {exc}")

    def start_category_processing(self) -> None:
        self._start_processing(integral_mode=False, force_selection_mode="categorias")

    def start_folio_processing(self) -> None:
        self._start_processing(integral_mode=False, force_selection_mode="folhas")

    def start_integral_processing(self) -> None:
        self._start_processing(integral_mode=True, force_selection_mode=None)

    def start_batch_only_processing(self) -> None:
        self._start_processing(integral_mode=False, force_selection_mode=None, batch_only_mode=True)

    def _start_processing(self, integral_mode: bool, force_selection_mode: str | None, batch_only_mode: bool = False) -> None:
        if self.processing_thread and self.processing_thread.is_alive():
            messagebox.showinfo("Em execucao", "Ja existe um processamento em andamento.")
            return

        zip_path = Path(self.zip_var.get().strip())
        output_dir = parse_output_dir(self.output_var.get())

        if not zip_path.exists():
            messagebox.showerror("Erro", "Selecione um arquivo ZIP valido.")
            return

        if zip_path.suffix.lower() != ".zip":
            messagebox.showerror("Erro", "O arquivo selecionado precisa ter extensao .zip.")
            return

        batch_max_size_mb = DEFAULT_BATCH_MAX_SIZE_MB
        if batch_only_mode:
            raw_batch_max_mb = self.batch_max_size_mb_var.get().strip()
            if not raw_batch_max_mb:
                batch_max_size_mb = 0
            else:
                try:
                    batch_max_size_mb = int(raw_batch_max_mb)
                except Exception:
                    messagebox.showerror("Erro", "Informe um numero inteiro valido para 'Max MB/lote'.")
                    return
                if batch_max_size_mb < 0:
                    messagebox.showerror("Erro", "'Max MB/lote' nao pode ser negativo.")
                    return

        if not integral_mode and not batch_only_mode:
            if force_selection_mode == "categorias":
                preprocess_ok = self.preprocess_categories()
            elif force_selection_mode == "folhas":
                preprocess_ok = self.preprocess_folios()
            else:
                preprocess_ok = False

            if not preprocess_ok:
                self.append_log("Processamento cancelado: nenhuma selecao seletiva foi definida.")
                return

        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            crop_signature_strip_width = float(str(self.crop_signature_strip_width_var.get()).strip().replace(",", "."))
        except Exception:
            messagebox.showerror("Erro", "Largura do corte da tarja deve ser numero.")
            return
        if crop_signature_strip_width <= 0:
            messagebox.showerror("Erro", "Largura do corte da tarja deve ser maior que zero.")
            return

        config = ProcessingConfig(
            zip_path=zip_path,
            output_dir=output_dir,
            main_rules=self.main_rules,
            main_categories_override=(
                None if integral_mode or self.selection_mode == "folhas" else set(self.selected_main_categories)
            ),
            main_file_indexes_override=(
                None if integral_mode or self.selection_mode != "folhas" else set(self.selected_main_file_indexes)
            ),
            integral_mode=integral_mode,
            batch_only_mode=batch_only_mode,
            batch_max_size_mb=batch_max_size_mb,
            replace_photo_pages=bool(self.replace_photo_pages_var.get()),
            crop_signature_strip_right=bool(self.crop_signature_strip_right_var.get()),
            crop_signature_strip_width_points=crop_signature_strip_width,
        )

        self.run_category_button.configure(state="disabled")
        self.run_folios_button.configure(state="disabled")
        self.run_integral_button.configure(state="disabled")
        self.run_batch_only_button.configure(state="disabled")
        if batch_only_mode:
            self.status_var.set("Processando (lotes IA)...")
        else:
            self.status_var.set("Processando (integral)..." if integral_mode else "Processando (seletivo)...")
        if integral_mode:
            self.append_log("Iniciando processamento integral (MONK): todas as pecas serao tratadas como principais.")
        elif batch_only_mode:
            self.append_log("Iniciando processamento dedicado de lotes IA (separado dos demais modos)...")
        else:
            if self.selection_mode == "folhas":
                self.append_log("Iniciando processamento seletivo por folhas...")
            else:
                self.append_log("Iniciando processamento seletivo por categorias...")
        if config.replace_photo_pages:
            self.append_log("Filtro de fotografias: ativo (paginas de foto serao substituidas por marcador).")
        else:
            self.append_log("Filtro de fotografias: desativado (paginas originais serao mantidas).")
        if config.crop_signature_strip_right:
            self.append_log(
                "Recorte de tarja lateral: ativo "
                f"({config.crop_signature_strip_width_points:.1f} pt na margem direita)."
            )
        else:
            self.append_log("Recorte de tarja lateral: desativado.")

        def worker() -> None:
            try:
                result = process_zip(config, log=lambda msg: self.worker_queue.put(("log", msg)))
                self.worker_queue.put(("done", result))
            except Exception:
                self.worker_queue.put(("error", traceback.format_exc()))

        self.processing_thread = threading.Thread(target=worker, daemon=True)
        self.processing_thread.start()

    def _poll_worker_queue(self) -> None:
        while True:
            try:
                event_type, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "log":
                self.append_log(str(payload))
            elif event_type == "done":
                result = payload
                assert isinstance(result, ProcessingResult)
                self.last_output_root = result.output_root
                self.refresh_results_view(result.output_root)
                self.run_category_button.configure(state="normal")
                self.run_folios_button.configure(state="normal")
                self.run_integral_button.configure(state="normal")
                self.run_batch_only_button.configure(state="normal")
                self.status_var.set("Concluido")
                if result.processing_mode == "integral":
                    mode_label = "Integral (MONK)"
                elif result.processing_mode == "lotes_ia":
                    mode_label = "Lotes IA (separado)"
                else:
                    mode_label = "Seletivo"

                if result.selection_criteria == "integral":
                    selection_label = "todas (integral)"
                elif result.selection_criteria == "lotes_ia":
                    selection_label = "nao se aplica (processamento dedicado)"
                elif result.selection_criteria == "folhas":
                    selection_label = f"{len(self.selected_main_file_indexes)} pecas (selecao por folhas)"
                elif result.selection_criteria == "categorias":
                    selection_label = f"{len(self.selected_main_categories)} categorias"
                else:
                    selection_label = "regras automaticas (sem pre-processamento)"

                if result.processing_mode == "lotes_ia":
                    summary = (
                        f"Modo: {mode_label}\n"
                        f"Concluido: {result.total_files} arquivos no ZIP | "
                        f"PDFs considerados: {result.main_files} | "
                        f"Arquivos nao PDF: {result.auxiliary_files}\n"
                        f"Saida: {result.output_root}\n"
                        f"Lotes IA: {result.batch_total}"
                    )
                    if result.batch_output_root is not None:
                        summary += f"\nPasta de lotes: {result.batch_output_root}"
                    if result.batch_full_summary_json_path is not None:
                        summary += f"\nSumario completo dos lotes (JSON): {result.batch_full_summary_json_path}"
                else:
                    summary = (
                        f"Modo: {mode_label}\n"
                        f"Concluido: {result.total_files} arquivos | "
                        f"Pecas principais: {result.main_files} | "
                        f"Documentos auxiliares: {result.auxiliary_files}\n"
                        f"Criterio de selecao das pecas principais: {selection_label}\n"
                        f"Saida: {result.output_root}\n"
                        f"PDF para IA: {result.ia_pdf_path or 'nao gerado'}\n"
                        f"Paginas no PDF IA: {result.ia_pdf_total_pages}\n"
                        f"Sumario completo (JSON): {result.full_summary_json_path}"
                    )
                    if result.selected_summary_json_path is not None:
                        summary += f"\nSumario (pecas selecionadas - JSON): {result.selected_summary_json_path}"
                    if result.selected_summary_md_path is not None:
                        summary += f"\nSumario (pecas selecionadas - MD): {result.selected_summary_md_path}"
                self.append_log(summary)
                messagebox.showinfo("Processamento concluido", summary)
            elif event_type == "error":
                self.run_category_button.configure(state="normal")
                self.run_folios_button.configure(state="normal")
                self.run_integral_button.configure(state="normal")
                self.run_batch_only_button.configure(state="normal")
                self.status_var.set("Erro")
                error_text = str(payload)
                self.append_log("Erro durante o processamento.")
                self.append_log(error_text)
                messagebox.showerror("Erro", error_text)

        self.after(200, self._poll_worker_queue)


def run_cli(args: argparse.Namespace) -> int:
    rules_path = Path(args.rules_file)
    rules = load_rules(rules_path)
    crop_width_points = float(getattr(args, "crop_signature_strip_width_points", DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS))
    if crop_width_points <= 0:
        raise ValueError("Parametro --signature-strip-width-pt deve ser maior que zero.")

    config = ProcessingConfig(
        zip_path=Path(args.zip),
        output_dir=parse_output_dir(args.output),
        main_rules=rules,
        batch_only_mode=bool(args.batch_only),
        batch_max_size_mb=max(0, int(args.batch_max_mb)),
        batch_cut_mode=_normalize_batch_cut_mode(getattr(args, "batch_cut_mode", DEFAULT_BATCH_CUT_MODE)),
        batch_target_tokens=max(1, int(getattr(args, "batch_target_tokens", DEFAULT_BATCH_TARGET_TOKENS))),
        batch_min_tokens=max(1, int(getattr(args, "batch_min_tokens", DEFAULT_BATCH_MIN_TOKENS))),
        batch_max_tokens=max(1, int(getattr(args, "batch_max_tokens", DEFAULT_BATCH_MAX_TOKENS))),
        replace_photo_pages=bool(getattr(args, "replace_photo_pages", True)),
        crop_signature_strip_right=bool(getattr(args, "crop_signature_strip_right", False)),
        crop_signature_strip_width_points=crop_width_points,
    )

    result = process_zip(config, log=print)
    print("---")
    print(f"Saida: {result.output_root}")
    print(f"Modo: {result.processing_mode}")
    print(f"Arquivos: {result.total_files}")
    print(f"Pecas principais: {result.main_files}")
    print(f"Documentos auxiliares: {result.auxiliary_files}")
    print(f"Filtro fotografias: {'ativo' if config.replace_photo_pages else 'desativado'}")
    if config.crop_signature_strip_right:
        print(f"Recorte tarja lateral: ativo ({config.crop_signature_strip_width_points:.1f} pt)")
    else:
        print("Recorte tarja lateral: desativado")
    if result.processing_mode == "lotes_ia":
        batch_mode = _normalize_batch_cut_mode(getattr(args, "batch_cut_mode", DEFAULT_BATCH_CUT_MODE))
        if batch_mode == BATCH_CUT_MODE_TOKENS:
            print(
                "Parametros de loteamento: "
                f"modo=tokens alvo={max(1, int(getattr(args, 'batch_target_tokens', DEFAULT_BATCH_TARGET_TOKENS)))} "
                f"faixa={max(1, int(getattr(args, 'batch_min_tokens', DEFAULT_BATCH_MIN_TOKENS)))}-"
                f"{max(1, int(getattr(args, 'batch_max_tokens', DEFAULT_BATCH_MAX_TOKENS)))}"
            )
        else:
            print(f"Parametros de loteamento: modo=mb max_mb={max(0, int(args.batch_max_mb))}")
        if result.batch_output_root is not None:
            print(f"Lotes IA: {result.batch_total} | Pasta: {result.batch_output_root}")
        if result.batch_full_summary_json_path is not None:
            print(f"Sumario completo dos lotes (JSON): {result.batch_full_summary_json_path}")
    else:
        print(f"PDF para IA: {result.ia_pdf_path or 'nao gerado'}")
        print(
            "PDFs incluidos: "
            f"{result.ia_pdf_inputs} | ignorados: {result.ia_pdf_skipped} | paginas: {result.ia_pdf_total_pages}"
        )
        print(f"Sumario completo (JSON): {result.full_summary_json_path}")
        if result.selected_summary_json_path is not None:
            print(f"Sumario (pecas selecionadas - JSON): {result.selected_summary_json_path}")
        if result.selected_summary_md_path is not None:
            print(f"Sumario (pecas selecionadas - MD): {result.selected_summary_md_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Organizador de ZIP processual")
    parser.add_argument("--zip", help="Caminho do arquivo ZIP")
    default_out = default_output_dir()
    parser.add_argument("--output", default=str(default_out), help=f"Pasta de saida (padrao: {default_out})")
    parser.add_argument("--rules-file", default=str(RULES_FILE), help="Arquivo JSON de regras")
    parser.add_argument(
        "--batch-only",
        action="store_true",
        help="Executa apenas o processamento dedicado de lotes IA (separado dos demais modos)",
    )
    parser.add_argument(
        "--batch-max-mb",
        type=int,
        default=DEFAULT_BATCH_MAX_SIZE_MB,
        help="Tamanho maximo aproximado do PDF por lote em MB (0 = sem limite)",
    )
    parser.add_argument(
        "--batch-cut-mode",
        choices=[BATCH_CUT_MODE_MB, BATCH_CUT_MODE_TOKENS],
        default=DEFAULT_BATCH_CUT_MODE,
        help="Modo de corte dos lotes: mb ou tokens.",
    )
    parser.add_argument(
        "--batch-target-tokens",
        type=int,
        default=DEFAULT_BATCH_TARGET_TOKENS,
        help="Alvo de tokens por lote (usado quando --batch-cut-mode=tokens).",
    )
    parser.add_argument(
        "--batch-min-tokens",
        type=int,
        default=DEFAULT_BATCH_MIN_TOKENS,
        help="Minimo de tokens por lote antes de antecipar split.",
    )
    parser.add_argument(
        "--batch-max-tokens",
        type=int,
        default=DEFAULT_BATCH_MAX_TOKENS,
        help="Teto de tokens por lote (hard cap).",
    )
    parser.add_argument(
        "--replace-photo-pages",
        dest="replace_photo_pages",
        action="store_true",
        default=True,
        help=(
            "Substitui paginas de fotografias por marcador textual "
            "(preserva a paginacao original do PDF)."
        ),
    )
    parser.add_argument(
        "--keep-photo-pages",
        dest="replace_photo_pages",
        action="store_false",
        help="Mantem paginas de fotografias no PDF IA (desativa o marcador de fotografia).",
    )
    parser.add_argument(
        "--crop-signature-strip-right",
        dest="crop_signature_strip_right",
        action="store_true",
        default=False,
        help="Recorta a lateral direita para remover a tarja padrao de assinatura do TJSP.",
    )
    parser.add_argument(
        "--signature-strip-width-pt",
        type=float,
        default=DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS,
        dest="crop_signature_strip_width_points",
        help=f"Largura do recorte da lateral direita em pontos (padrao: {DEFAULT_SIGNATURE_STRIP_RIGHT_CROP_POINTS}).",
    )
    args = parser.parse_args()

    if args.zip:
        return run_cli(args)

    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
