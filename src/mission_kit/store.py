"""原子持久化状态存储。

磁盘布局（状态目录）::

    state.json          单一状态文件（仅经 temp + os.replace 整体替换）
    sealed/<sha>.json   超前/不可解释文档的原文封存件

所有库存变动都先在内存完成校验与提交，再把追加后的状态**整体原子替换**落盘；
因此崩溃恢复时磁盘上要么是旧状态、要么是完整新状态，绝不会出现半条交接。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def _default_json(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化的对象：{type(obj)!r}")


class StateStore:
    """读写任务状态；写入一律原子替换。"""

    STATE_FILENAME = "state.json"
    SEALED_DIRNAME = "sealed"

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / self.SEALED_DIRNAME).mkdir(exist_ok=True)
        self._state_path = self.dir / self.STATE_FILENAME

    # ------------------------------------------------------------------ #
    def load(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return self._blank()
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        """原子写入：同目录临时文件 + os.replace，保证不会读到半截 JSON。"""

        fd, tmp_name = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2,
                          sort_keys=True, default=_default_json)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._state_path)
        except BaseException:
            # 落盘失败必须清掉临时文件，绝不留下可能被误认的状态残骸。
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {
            # 有序重放日志：每项是一次已确认的领域操作（交接/检查/替代签发）。
            "journal": [],
            # record_id -> 接纳留痕（内容摘要、采用的合同版本、迁移来源）。
            "applied": {},
            # 超前/不可解释文档封存索引。
            "sealed": [],
            # event_id -> 离线回执处理结果（去重/冲突判定依据）。
            "events": {},
            # event_id -> 回执处置留痕（accepted/sealed + 载荷摘要，用于乱序重复去重）。
            "receipts": {},
            # 保管链缺口导致暂缓的回执（前置回执到达后按序重试）。
            "pending": [],
            # 已核验校准冻结到的墙钟时点（恢复后据此重放冻结）。
            "freeze_checked_at": None,
            # 检查记录（校准冻结后仍须保留）。
            "examinations": [],
            # 替代设备签发记录（按冻结序列号幂等）。
            "replacements": [],
            # 待人工复核的冲突。
            "review": [],
            # 已发出通知的去重键。
            "notified": [],
        }

    # ------------------------------------------------------------------ #
    def seal_raw(self, digest: str, raw_text: str) -> str:
        """把封存原文独立落盘（同样原子替换），返回封存文件相对名。"""

        rel = f"{self.SEALED_DIRNAME}/{digest}.json"
        target = self.dir / rel
        if not target.exists():
            fd, tmp_name = tempfile.mkstemp(
                prefix=".sealed-", suffix=".tmp", dir=str(self.dir / self.SEALED_DIRNAME)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(raw_text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, target)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
        return rel
