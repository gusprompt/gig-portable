"""
Compatibilidade legada para o motor estrutural.

Historicamente, o mapa estrutural compartilhado morava neste modulo. A
implementacao real agora foi extraida para `gig.structural_map_engine`, e este
arquivo permanece como fachada para evitar quebra de imports antigos.
"""

from .structural_map_engine import (
    DEPURATION_LEVEL_AGGRESSIVE,
    DEPURATION_LEVEL_CONSERVATIVE,
    DEPURATION_LEVEL_NONE,
    OUTPUT_FILENAME,
    SCHEMA_NAME,
    PilarResult,
    StructuralMapResult,
    build_structural_map,
)

__all__ = [
    "SCHEMA_NAME",
    "OUTPUT_FILENAME",
    "DEPURATION_LEVEL_NONE",
    "DEPURATION_LEVEL_CONSERVATIVE",
    "DEPURATION_LEVEL_AGGRESSIVE",
    "StructuralMapResult",
    "PilarResult",
    "build_structural_map",
]
