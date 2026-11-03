from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import Owner
from backend.intelligence.analysis import MAX_INVOICE_CHARS, extract_invoice

router = APIRouter(prefix="/api/invoices", tags=["intelligence"])


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=MAX_INVOICE_CHARS * 2)]


class InvoiceOut(BaseModel):
    result: dict[str, Any]
    source: Literal["model", "template"]


@router.post("/extract", response_model=InvoiceOut)
async def extract(body: InvoiceIn, _owner: Owner) -> InvoiceOut:
    answer = await extract_invoice(body.text)
    return InvoiceOut(result=answer["result"], source=answer["source"])