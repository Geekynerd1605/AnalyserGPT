from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    case_id: str = Field(..., description="Unique identifier for the eval case.")
    dataset: str = Field(..., description="Path to CSV file, relative to repo root.")
    prompt: str = Field(..., description="User question for the analyzer.")
    tags: list[str] = Field(default_factory=list, description="Optional case tags.")


class EvalCaseFile(BaseModel):
    cases: list[EvalCase]
