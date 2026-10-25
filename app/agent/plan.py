"""The plan the model submits through the `submit_plan` pseudo-tool."""

from pydantic import BaseModel, ConfigDict, Field


class CitedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=300)


class Plan(BaseModel):
    """An evidence-backed plan. Every cited file must have been read through `read_file`."""

    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1500, description="What is wrong and the fix.")
    cited_files: list[CitedFile] = Field(
        min_length=1, max_length=10, description="Files you read that this plan relies on."
    )
    steps: list[str] = Field(min_length=1, max_length=10, description="Steps of a minimal fix.")


SUBMIT_PLAN_DESCRIPTION = (
    "Submit your final plan. Cite only files you have read with read_file."
)
