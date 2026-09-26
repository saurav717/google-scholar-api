"""Self-hosted Google Scholar API with SerpAPI-compatible JSON."""

__version__ = "0.2.0"

from .client import BlockedError, FetchResult, NotFoundError, ScholarClient, ScholarError  # noqa: E402
from .engines import ENGINE_DOCS, ENGINES, ParamError, run  # noqa: E402

__all__ = [
    "ScholarClient", "FetchResult", "ScholarError", "BlockedError", "NotFoundError",
    "ParamError", "ENGINES", "ENGINE_DOCS", "run", "__version__",
]
