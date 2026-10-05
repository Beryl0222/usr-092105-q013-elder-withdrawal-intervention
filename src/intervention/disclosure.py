"""警方案情披露的分级授权模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LegalAuthorization:
    """警方调取案件信息的合法授权。

    covers 载明可调取范围："evidence"（完整证据）、"fund_flow"（资金去向）。
    """

    document_no: str
    issuing_authority: str
    scope: tuple[str, ...]  # 案件编号，或 ("*",) 表示全部在办案件
    issued_at: datetime
    expires_at: datetime
    covers: frozenset[str]


def validate_authorization(
    auth: LegalAuthorization, case_id: str, now: datetime
) -> list[str]:
    """校验授权文号、出具机关、有效期与调取范围。"""
    errors: list[str] = []
    if not auth.document_no:
        errors.append("授权文件缺少文号")
    if not auth.issuing_authority:
        errors.append("授权文件缺少出具机关")
    if now < auth.issued_at:
        errors.append("授权文件尚未生效")
    if now > auth.expires_at:
        errors.append("授权文件已过有效期")
    if "*" not in auth.scope and case_id not in auth.scope:
        errors.append("授权范围不包含本案")
    if not auth.covers:
        errors.append("授权文件未载明可调取的内容范围")
    return errors
