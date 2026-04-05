"""
Fachada compartilhada para o motor estrutural usado por Filtro e variantes canonicas.

Mantem compatibilidade com a implementacao atual em gig.pilar, mas desacopla os
chamadores do nome conceitual da ferramenta.
"""

from .structural_map_engine import PilarResult, StructuralMapResult, build_structural_map

__all__ = ["StructuralMapResult", "PilarResult", "build_structural_map"]
