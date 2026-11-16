from pydantic import BaseModel, Field


class ObservationInput(BaseModel):
    day: str = Field(max_length=20)
    team: str = Field(max_length=40)
    zone: str = Field(max_length=80)
    point_name: str = Field(default='', max_length=160)
    token: str = Field(default='', max_length=220)
    passed: bool | int | str
    note: str = Field(default='', max_length=500)
    source: str = Field(default='manual', max_length=200)


class MergeInput(BaseModel):
    source_token: str = Field(min_length=1, max_length=220)
    target_token: str = Field(min_length=1, max_length=220)
    reason: str = Field(default='', max_length=500)


class RevokeInput(BaseModel):
    reason: str = Field(default='', max_length=500)


class AdjudicationInput(BaseModel):
    decision: bool | str
    note: str = Field(default='', max_length=500)


class FrameInput(BaseModel):
    size: int = Field(ge=1, le=1_000_000_000)


class BatchInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)


class PublishInput(BaseModel):
    idempotency_key: str = Field(default='', max_length=160)
    interrupt_after_attempt: bool = False


class IdentityCorrectionInput(BaseModel):
    token: str = Field(default='', max_length=220)
    point_name: str = Field(default='', max_length=160)
    reason: str = Field(default='', max_length=500)
