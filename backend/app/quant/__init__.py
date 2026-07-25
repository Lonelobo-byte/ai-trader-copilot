"""Independent quantitative research components.

The package deliberately emits estimates and risk constraints, never execution
commands.  Each module is pure and can be replaced by a production data/model
adapter without changing the API orchestration layer.
"""

from .engine import build_quantitative_assessment

__all__ = ["build_quantitative_assessment"]
