"""Authenticated BYOK controls for OpenRouter."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db.models import User
from ..user_ai import (
    UserAIConnectionError,
    delete_user_connection,
    get_user_connection,
    public_connection,
    save_openrouter_connection,
)

router = APIRouter(prefix="/ai-connection", tags=["AI connection"])


class OpenRouterConnectionRequest(BaseModel):
    api_key: str = Field(default="", max_length=1000)
    model: str = Field(min_length=3, max_length=200)


@router.get("")
async def connection_status(user: User = Depends(current_user)):
    return public_connection(await get_user_connection(user.id))


@router.put("/openrouter")
async def save_openrouter_connection_route(
    request: OpenRouterConnectionRequest,
    user: User = Depends(current_user),
):
    try:
        return await save_openrouter_connection(user.id, api_key=request.api_key, model=request.model)
    except UserAIConnectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/openrouter", status_code=status.HTTP_204_NO_CONTENT)
async def remove_openrouter_connection(user: User = Depends(current_user)):
    await delete_user_connection(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
