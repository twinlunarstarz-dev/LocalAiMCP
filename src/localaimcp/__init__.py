"""LocalAI MCP server."""

__version__ = "0.1.0"

# Apply the fully reviewed semantic name catalog before any caller enumerates
# Swagger operations. `spec.py` still retains its fallback generator so a future
# LocalAI endpoint can be exposed even before it receives an explicit reviewed name.
from . import spec as _spec
from .tool_names import TOOL_NAME_OVERRIDES as _REVIEWED_TOOL_NAMES

_spec._TOOL_NAME_OVERRIDES.update(_REVIEWED_TOOL_NAMES)
