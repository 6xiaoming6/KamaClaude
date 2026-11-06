from __future__ import annotations

from pydantic import Field, field_validator

from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class FinishParams(ToolParams):
    result: str = Field(min_length=1, description="Complete final reply or question for the user.")

    @field_validator("result")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("result must contain a non-whitespace reply")
        return value


class FinishTool(BaseTool):
    name = "finish"
    description = (
        "Submit this Run's final reply and return control to the user. Pass only result, "
        "which may be an answer or a question. Call this tool alone after observing all "
        "business tool results. Report pending work honestly. Submission is not "
        "independent verification of task correctness."
    )
    params_model = FinishParams
    input_schema = FinishParams.model_json_schema()
    retry_safe = True
    ends_run = True

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = FinishParams.model_validate(params)
        # 这里只返回控制信号；执行器将配对结果、游标和 Run 终态一起提交。
        return ToolResult(p.result, preserve_in_context=True, ends_run=True)
