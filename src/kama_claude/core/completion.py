"""本轮显式提交协议；只定义提示，不维护另一份执行状态。"""

FINISH_REMINDER = (
    "[KamaClaude finish protocol] Your last response did not submit this Run. "
    "Continue using tools if work remains, or call finish alone with a nonempty result. "
    "The result may be the final answer or a question for the user. "
    "Do not repeat the answer as ordinary text."
)
FINISH_INSTRUCTIONS = (
    "\n\n## End-of-Run protocol\n"
    "To return control to the user, call finish alone with the complete result string. "
    "Use it for a completed answer or a question requiring user input. "
    "It ends this Run, not the whole chat, and is not proof of business acceptance. "
    "Include the actual outcome, verification and remaining uncertainty when relevant. "
    "Do not declare completion only in ordinary text, and do not combine finish with other "
    "tool calls. Observe business tool results and report unfinished background work honestly. "
    "Do not duplicate the final answer outside finish."
    " Collect every background subagent result with agent_result before finish. "
    "Cancel unneeded child work with agent_cancel and collect its terminal result."
)
