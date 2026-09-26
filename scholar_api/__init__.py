"""Self-hosted Google Scholar API with SerpAPI-compatible JSON."""

from .client import BlockedError, ScholarClient, ScholarError
from .engines import ENGINES, ParamError, run

__all__ = ["ScholarClient", "ScholarError", "BlockedError", "ParamError", "ENGINES", "run"]
