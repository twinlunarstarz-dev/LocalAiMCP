"""LocalAI MCP server."""

__version__ = "0.1.0"

# Apply the fully reviewed semantic name catalog before any caller enumerates
# Swagger operations. `spec.py` retains its fallback generator for future routes.
from . import spec as _spec
from .tool_names import TOOL_NAME_OVERRIDES as _REVIEWED_TOOL_NAMES

_spec._TOOL_NAME_OVERRIDES.update(_REVIEWED_TOOL_NAMES)

# Add conservative guidance for Swagger fields whose upstream descriptions are
# blank or low-information. Real Swagger descriptions still take precedence.
from . import metadata as _metadata
from .field_hints import FIELD_HINTS as _EXTRA_FIELD_HINTS

_metadata._FIELD_HINTS.update(_EXTRA_FIELD_HINTS)
