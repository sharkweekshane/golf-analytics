"""Pydantic models for LLM structured outputs.

Design rules (from docs/RESEARCH_PLAN.md §3-4):
- Every field required, nothing Optional/Union: unknowns use sentinels (-1, "", or an explicit enum
  value). This keeps the wire schemas far under the structured-output limits on optional/union params.
- Models inherit `Lenient`, which normalises enum case/spacing drift ("Not Visible" -> "not_visible")
  before validation, so one sloppy token doesn't throw away an otherwise-good extraction.
"""
from __future__ import annotations

from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator


def _norm_literal(value: Any, allowed: tuple) -> Any:
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "_").replace("-", "_")
        if s in allowed:
            return s
    return value


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _normalise_enums(cls, value: Any, info: ValidationInfo) -> Any:
        field = cls.model_fields.get(info.field_name) if info.field_name else None
        if field is None:
            return value
        ann = field.annotation
        if get_origin(ann) is Literal:
            return _norm_literal(value, get_args(ann))
        if get_origin(ann) is list and isinstance(value, list):
            args = get_args(ann)
            if args and get_origin(args[0]) is Literal:
                allowed = get_args(args[0])
                return [_norm_literal(v, allowed) for v in value]
        return value


Conf = Literal["high", "medium", "low"]
