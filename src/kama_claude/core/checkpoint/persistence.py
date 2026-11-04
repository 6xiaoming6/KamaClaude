from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.checkpoint import Checkpoint
    from kama_claude.core.context import ExecutionContext
    from kama_claude.core.session.model import Session
    from kama_claude.core.session.store import SessionStore


# 绑定本轮执行与会话存储，提交时直接使用唯一的 Run 状态
class RunPersistence:
    # 保存本轮上下文和存储绑定，不复制或维护第二份执行状态
    def __init__(
        self,
        context: ExecutionContext,
        checkpoint: Checkpoint,
        session: Session,
        store: SessionStore,
    ) -> None:
        self.context = context
        self.checkpoint = checkpoint
        self.session = session
        self.store = store

    # 将消息、同一个 Run 对象和会话元数据一起原子提交
    def save(self) -> None:
        # 压缩可能替换整个列表，每次保存都绑定当前工作历史
        self.checkpoint.messages = self.context.messages
        # 复用唯一 Run 对象，不逐字段复制或维护另一份状态
        self.checkpoint.run = self.context.run
        self.checkpoint.session = self.session.to_dict()
        self.store.checkpoints.save(self.checkpoint)

    # 保留原有 thread.jsonl 历史输出，导出不替代检查点提交
    def export_history(self) -> None:
        self.store.export_meta(self.session)
        self.store.export_messages(
            self.session.id, self.context.messages, self.context.run.run_id,
        )
