"""Repository-scoped question endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from codebase_intelligence.api.dependencies import get_rag_service
from codebase_intelligence.models import QuestionRequest, QuestionResponse
from codebase_intelligence.rag_service import RAGService

router = APIRouter(prefix="/repositories", tags=["questions"])


@router.post("/{repository_id}/questions", response_model=QuestionResponse)
async def ask_question(
    repository_id: str,
    payload: QuestionRequest,
    rag_service: Annotated[RAGService, Depends(get_rag_service)],
) -> QuestionResponse:
    return await rag_service.ask(repository_id, payload)
