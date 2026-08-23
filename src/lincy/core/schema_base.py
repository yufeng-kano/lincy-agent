"""Shared strict base model for all config schemas."""

from pydantic import BaseModel, ConfigDict


class StrictConfigModel(BaseModel):
    """Shared strict config model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")
