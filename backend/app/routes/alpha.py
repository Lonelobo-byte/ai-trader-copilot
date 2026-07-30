"""Alpha Research Engine API Router.

Exposes endpoints for hypothesis registry, candidates validation, and dynamically
calculated information coefficients (IC) / edge decay on historical databases.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import TradeSignal
from app.brains.signal_lifecycle import TERMINAL_SIGNAL_STATUSES
from app.quant.research import list_hypotheses, validate_series
from app.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/alpha", tags=["Alpha Research Engine"])


class ValidationRequest(BaseModel):
    feature: list[float] = Field(..., min_length=8, max_length=10_000, description="Array of feature measurements.")
    future_returns: list[float] = Field(..., min_length=8, max_length=10_000, description="Aligned forward returns corresponding to each feature period.")
    train_fraction: float = Field(0.7, gt=0.5, lt=0.9, description="Fraction of series to use for training.")


@router.get("/hypotheses")
def get_hypotheses():
    """Retrieve the registered alpha hypothesis dossier and validation rules."""
    return list_hypotheses()


@router.post("/validate")
def post_validate_series(req: ValidationRequest, request: Request):
    """Validate a custom features candidate series against future return lags."""
    try:
        enforce_rate_limit(request, "alpha_validate", limit=10, window_seconds=60)
        return validate_series(req.feature, req.future_returns, train_fraction=req.train_fraction)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The feature series could not be validated. Ensure both arrays are aligned numeric observations.") from exc


@router.get("/report")
async def get_alpha_report():
    """Analyze historical session results to discover repeatable market edges.

    Calculates correlation coefficients, win rates by regime type, and edge stability.
    """
    async with AsyncSessionLocal() as session:
        stmt = select(TradeSignal).where(TradeSignal.status.in_(TERMINAL_SIGNAL_STATUSES))
        res = await session.execute(stmt)
        records = res.scalars().all()

    if not records:
        return {
            "status": "insufficient_data",
            "sessions_analyzed": 0,
            "message": "No historical outcomes found. Seed the database to view alpha metrics.",
            "edges": {},
        }

    # Extract features and map binary success (1.0 for SUCCESS, 0.0 for FAILURE)
    outcomes = []
    execution_flow_scores = []
    funding_rates = []
    causal_alignments = []

    regime_stats: dict[str, dict[str, int]] = {}

    for r in records:
        success = 1.0 if r.status == "COMPLETED" else 0.0
        outcomes.append(success)

        context = r.context if isinstance(r.context, dict) else {}
        review = r.ai_review if isinstance(r.ai_review, dict) else {}
        reviewed_features = (
            review.get("full_quant_features")
            or review.get("quant_features")
            or {}
        )
        causal_context = (
            context.get("causal_market_context")
            or reviewed_features.get("market_context")
            or {}
        )
        order_flow = (
            (causal_context.get("components") or {}).get("order_flow")
            or {}
        )
        execution_flow_scores.append(float(order_flow.get("score") or 0.0))

        # Extract funding
        derivatives = reviewed_features.get("derivatives") or {}
        fund = float(derivatives.get("funding_rate") or 0.0)
        funding_rates.append(fund)

        direction = str(causal_context.get("direction") or "WAIT")
        side = str(r.side or "")
        causal_alignments.append(
            1.0
            if (side == "LONG" and direction == "LONG")
            or (side == "SHORT" and direction == "SHORT")
            else 0.0
        )

        # Group by regime
        reg = str(
            (
                (causal_context.get("components") or {})
                .get("regime_structure", {})
                .get("evidence")
            )
            or direction
            or "unknown"
        )
        if reg not in regime_stats:
            regime_stats[reg] = {"success": 0, "total": 0}
        regime_stats[reg]["total"] += 1
        if r.status == "COMPLETED":
            regime_stats[reg]["success"] += 1

    import numpy as np

    def get_correlation(x_arr: list[float], y_arr: list[float]) -> float:
        if len(x_arr) < 5 or np.std(x_arr) == 0 or np.std(y_arr) == 0:
            return 0.0
        return float(np.corrcoef(x_arr, y_arr)[0, 1])

    # Calculate information coefficients
    ic_execution_flow = get_correlation(execution_flow_scores, outcomes)
    ic_funding = get_correlation(funding_rates, outcomes)
    ic_causal_alignment = get_correlation(causal_alignments, outcomes)

    # Compile regime outcomes win-rates
    regime_report = {}
    for name, stats in regime_stats.items():
        win_rate = stats["success"] / stats["total"] if stats["total"] > 0 else 0.0
        regime_report[name] = {
            "observations": stats["total"],
            "win_rate": round(win_rate, 4),
        }

    return {
        "status": "active",
        "sessions_analyzed": len(records),
        "regime_performance": regime_report,
        "discovered_edges": {
            "actual_execution_flow": {
                "information_coefficient": round(ic_execution_flow, 4),
                "predictive_edge": "positive_correlation" if ic_execution_flow > 0.05 else "weak_or_none",
                "notes": "Tests normalized aggressor flow and price acceptance, not displayed-book intent.",
            },
            "funding_divergence": {
                "information_coefficient": round(ic_funding, 4),
                "predictive_edge": "positive_correlation" if abs(ic_funding) > 0.05 else "weak_or_none",
            },
            "causal_context_alignment": {
                "information_coefficient": round(ic_causal_alignment, 4),
                "predictive_edge": "positive_correlation" if ic_causal_alignment > 0.05 else "weak_or_none",
            }
        },
        "edge_decay_coefficient": None,
        "edge_decay_status": "not_measured",
        "edge_decay_note": "No decay estimate is reported until it is measured from timestamped real observations.",
    }
