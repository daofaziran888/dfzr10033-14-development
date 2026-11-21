"""实验共享指标的公共计算：分桶、曝光锁定、角色授权范围和口径复算。

这些函数只依赖内存仓库，既被实验分组指标服务使用，也被指标工厂和共享报告复用。
所有对外口径都必须经过 ``authorized_groups`` / ``aggregate_scope`` 重新计算，
不能直接把全量曝光或全量小计透出给读者。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable

from app.store import store

BUCKET_TABLE = "exp_bucket_config"
EXPOSURE_TABLE = "exp_exposure"
DETAIL_TABLE = "exp_metric_detail"
GROUP_METRIC_TABLE = "exp_group_metric"
SHARE_TABLE = "exp_group_share"
ACCESS_TABLE = "exp_role_access"
REPORT_TABLE = "exp_report"
DELIVERY_TABLE = "exp_report_delivery"
RELEASE_TABLE = "exp_release"

PUBLIC = "公开"
RESTRICTED = "受限"
EXITED = "已退出"
SHARE_SCOPES = (PUBLIC, RESTRICTED, EXITED)

DEFAULT_ROLE = "analyst"
PUBLISH_ROLES = {"experiment_owner", "data_steward", "admin", "metric_engine"}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def normalize_share_scope(value: Any) -> str:
    raw = str(value or PUBLIC).strip()
    mapping = {
        "公开": PUBLIC, "共享": PUBLIC, "可共享": PUBLIC, "public": PUBLIC,
        "shared": PUBLIC, "open": PUBLIC,
        "受限": RESTRICTED, "限制": RESTRICTED, "内部": RESTRICTED,
        "restricted": RESTRICTED, "private": RESTRICTED,
        "已退出": EXITED, "退出": EXITED, "收回": EXITED,
        "exited": EXITED, "withdrawn": EXITED,
    }
    return mapping.get(raw.casefold() if raw.isascii() else raw, raw if raw in SHARE_SCOPES else RESTRICTED)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().casefold() in {
        "1", "true", "yes", "y", "是", "已转化", "转化", "on",
    }


def split_scopes(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = str(value or "").replace("，", ",").replace("、", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


def split_groups(value: Any) -> list[str]:
    return [part.strip() for part in split_scopes(value)]


def role_profile(role: str | None) -> dict[str, Any]:
    """读取角色可读共享范围；未知角色按最小权限处理，只能读公开组。"""
    role = (role or DEFAULT_ROLE).strip()
    for row in store.rows(ACCESS_TABLE):
        if str(row.get("角色", "")).strip() == role:
            scopes = split_scopes(row.get("可读共享范围", PUBLIC))
            groups = split_groups(row.get("可读分组", ""))
            return {
                "role": role,
                "scopes": scopes or [PUBLIC],
                "groups": groups,
                "can_publish": truthy(row.get("可发布", "否")) or role in PUBLISH_ROLES,
            }
    return {
        "role": role,
        "scopes": [PUBLIC],
        "groups": [],
        "can_publish": role in PUBLISH_ROLES,
    }


def experiment_exists(experiment_id: str) -> bool:
    if any(str(row.get("实验编号", "")) == experiment_id for row in store.rows(BUCKET_TABLE)):
        return True
    if any(str(row.get("实验编号", "")) == experiment_id for row in store.rows(SHARE_TABLE)):
        return True
    return any(
        str(row.get("实验编号", "")) == experiment_id or str(row.get("id", "")) == experiment_id
        for row in store.rows("ab_config")
    )


def bucket_configs(experiment_id: str, version: str | None = None) -> list[dict[str, Any]]:
    rows = [
        row for row in store.rows(BUCKET_TABLE)
        if str(row.get("实验编号", "")) == experiment_id
    ]
    if version is not None:
        rows = [row for row in rows if str(row.get("分桶版本", "")) == version]
    return rows


def latest_version(experiment_id: str) -> str | None:
    rows = bucket_configs(experiment_id)
    if not rows:
        # 兼容只有分组共享记录、没有显式分桶配置的实验。
        rows = [
            row for row in store.rows(SHARE_TABLE)
            if str(row.get("实验编号", "")) == experiment_id
        ]
        versions = [str(row.get("生效版本", "")) for row in rows if row.get("生效版本")]
        return sorted(versions, key=_version_key)[-1] if versions else None
    versions = [str(row.get("分桶版本", "")) for row in rows if row.get("分桶版本")]
    return sorted(set(versions), key=_version_key)[-1] if versions else None


def _version_key(value: str) -> tuple[int, str]:
    raw = str(value or "v0").lstrip("vV")
    try:
        return (int(raw), str(value))
    except ValueError:
        return (0, str(value))


def group_configs(experiment_id: str, version: str | None = None) -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    for row in bucket_configs(experiment_id, version):
        name = str(row.get("分组名称", "")).strip()
        if name:
            configs[name] = row
    return configs


def current_shares(experiment_id: str) -> dict[str, str]:
    """分组当前的共享状态，独立于分桶版本；退出共享后所有版本立即不可共享。"""
    shares: dict[str, str] = {}
    for row in store.rows(SHARE_TABLE):
        if str(row.get("实验编号", "")) == experiment_id:
            name = str(row.get("分组名称", "")).strip()
            if name:
                shares[name] = normalize_share_scope(row.get("共享范围", RESTRICTED))
    # 没有显式共享记录的组沿用分桶配置里的值。
    for name, config in group_configs(experiment_id).items():
        shares.setdefault(name, normalize_share_scope(config.get("共享范围", RESTRICTED)))
    return shares


def share_of(experiment_id: str, group_name: str) -> str:
    return current_shares(experiment_id).get(group_name, RESTRICTED)


def group_type(experiment_id: str, group_name: str, version: str | None = None) -> str:
    config = group_configs(experiment_id, version).get(group_name, {})
    return str(config.get("分组类型", "") or "")


def stable_bucket(experiment_id: str, user_id: str, version: str) -> int:
    digest = hashlib.md5(f"{experiment_id}::{user_id}::{version}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def _bucket_range(value: Any) -> tuple[int, int] | None:
    text = str(value or "").replace("~", "-").replace("—", "-")
    if "-" not in text:
        return None
    left, _, right = text.partition("-")
    try:
        lo, hi = int(left.strip()), int(right.strip())
    except ValueError:
        return None
    return (lo, hi) if lo <= hi else (hi, lo)


def resolve_group(experiment_id: str, user_id: str, version: str) -> str | None:
    """按版本内的桶范围稳定分桶；同版本同用户永远得到同一个组。"""
    configs = list(group_configs(experiment_id, version).values())
    if not configs:
        return None
    point = stable_bucket(experiment_id, user_id, version)
    for config in configs:
        override_users = split_groups(config.get("指定用户", ""))
        if user_id in override_users:
            return str(config.get("分组名称", "")).strip()
    for config in configs:
        bounds = _bucket_range(config.get("归属桶范围", ""))
        if bounds and bounds[0] <= point <= bounds[1]:
            return str(config.get("分组名称", "")).strip()
    return str(configs[0].get("分组名称", "")).strip()


def exposures_for(
    experiment_id: str,
    version: str | None = None,
    group_names: Iterable[str] | None = None,
    exposure_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    groups = set(group_names) if group_names is not None else None
    ids = set(str(item) for item in exposure_ids) if exposure_ids is not None else None
    rows: list[dict[str, Any]] = []
    for row in store.rows(EXPOSURE_TABLE):
        if str(row.get("实验编号", "")) != experiment_id:
            continue
        if version is not None and str(row.get("分桶版本", "")) != version:
            continue
        if groups is not None and str(row.get("分组名称", "")) not in groups:
            continue
        code = str(row.get("曝光编号", ""))
        if ids is not None and code not in ids:
            continue
        rows.append(row)
    return rows


def exposure_ids(exposures: Iterable[dict[str, Any]]) -> list[str]:
    return sorted(str(row.get("曝光编号", "")) for row in exposures if row.get("曝光编号"))


def aggregate_scope(experiment_id: str, metric: str, exposure_ids_list: Iterable[str]) -> dict[str, Any]:
    """只按明确的曝光编号范围计算分母和转化量，不接受隐式全量范围。"""
    codes = set(str(item) for item in exposure_ids_list)
    exposures = {
        str(row.get("曝光编号", "")): row
        for row in store.rows(EXPOSURE_TABLE)
        if str(row.get("实验编号", "")) == experiment_id and str(row.get("曝光编号", "")) in codes
    }
    details = [
        row for row in store.rows(DETAIL_TABLE)
        if str(row.get("实验编号", "")) == experiment_id
        and str(row.get("指标名称", "")) == metric
        and str(row.get("曝光编号", "")) in codes
    ]
    detail_by_exposure: dict[str, list[dict[str, Any]]] = {}
    for row in details:
        detail_by_exposure.setdefault(str(row.get("曝光编号", "")), []).append(row)

    sample = 0
    conversions = 0
    group_name = ""
    bucket_version = ""
    for code in sorted(codes):
        exposure = exposures.get(code)
        if exposure is None:
            # 范围里有无法解析的曝光编号时不计入，避免把未知样本算进分母。
            continue
        group_name = group_name or str(exposure.get("分组名称", ""))
        bucket_version = bucket_version or str(exposure.get("分桶版本", ""))
        rows = detail_by_exposure.get(code)
        if rows:
            for row in rows:
                try:
                    sample += int(row.get("样本数", 1) or 0)
                    conversions += int(row.get("转化数", 0) or 0)
                except (TypeError, ValueError):
                    continue
        else:
            sample += _to_int(exposure.get("样本数", 1), 1)
            conversions += _to_int(exposure.get("转化数", exposure.get("转化", 0)), 0)
    return {
        "分组名称": group_name,
        "分桶版本": bucket_version,
        "样本量": sample,
        "转化量": conversions,
        "转化率": safe_rate(conversions, sample),
        "曝光数": len(exposures),
        "曝光范围": ",".join(sorted(codes)),
    }


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def aggregate_groups(
    experiment_id: str,
    metric: str,
    version: str | None = None,
    group_names: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    rows: dict[str, list[str]] = {}
    for exposure in exposures_for(experiment_id, version, group_names):
        rows.setdefault(str(exposure.get("分组名称", "")), []).append(str(exposure.get("曝光编号", "")))
    return {
        group: aggregate_scope(experiment_id, metric, codes)
        for group, codes in rows.items()
        if group
    }


def authorized_groups(
    experiment_id: str,
    version: str | None,
    role: str | None,
) -> dict[str, Any]:
    """返回角色实际可读的分组及被抑制原因；已退出组对任何角色都不可共享。"""
    profile = role_profile(role)
    shares = current_shares(experiment_id)
    configs = group_configs(experiment_id, version)
    names = list(dict.fromkeys([*configs.keys(), *shares.keys()]))
    visible: list[str] = []
    hidden: list[dict[str, str]] = []
    for name in names:
        share = shares.get(name, RESTRICTED)
        if share == EXITED:
            hidden.append({"分组名称": name, "原因": "已退出共享范围"})
            continue
        if share not in profile["scopes"]:
            hidden.append({"分组名称": name, "原因": "角色无该共享范围权限"})
            continue
        if profile["groups"] and name not in profile["groups"]:
            hidden.append({"分组名称": name, "原因": "不在角色可读分组名单"})
            continue
        visible.append(name)
    return {
        "role": profile["role"],
        "scopes": profile["scopes"],
        "groups": visible,
        "hidden": hidden,
        "has_hidden": bool(hidden),
        "profile": profile,
    }


def group_metric_exposure_scope(row: dict[str, Any]) -> list[str]:
    return split_groups(row.get("曝光范围", ""))


def write_release_entry(
    experiment_id: str,
    report: dict[str, Any],
    *,
    actor: str = "metric_engine",
) -> dict[str, Any]:
    """把实际开放的指标结果回写实验发布清单，标识版本、分组和抑制项。

    发布清单只承载报告里实际出现过的分组；受限组仅以名称出现在“受限分组”，
    不带任何样本量或转化量。
    """
    import json as _json

    open_groups = [
        {
            "分组名称": row.get("分组名称"),
            "样本量": row.get("样本量"),
            "转化量": row.get("转化量"),
            "转化率": row.get("转化率"),
            "曝光范围": row.get("曝光范围"),
        }
        for row in report.get("分组结果", [])
    ]
    restricted = [str(item.get("分组名称", "")) for item in report.get("受限分组", [])]
    payload = {
        "实验编号": experiment_id,
        "报告编号": report.get("报告编号"),
        "分桶版本": report.get("分桶版本"),
        "指标名称": report.get("指标名称"),
        "角色": report.get("角色"),
        "开放分组": "、".join(str(item.get("分组名称")) for item in open_groups),
        "开放指标": _json.dumps(open_groups, ensure_ascii=False),
        "受限分组": "、".join(restricted),
        "抑制项": "；".join(report.get("抑制项", [])),
        "更新时间": now(),
        "操作人": actor,
        "status": "已发布",
        "pending": False,
        "abnormal": bool(restricted),
    }
    rows = store.rows(RELEASE_TABLE)
    entry = next(
        (row for row in rows if str(row.get("实验编号", "")) == experiment_id),
        None,
    )
    if entry is None:
        entry = {"id": next_id(RELEASE_TABLE), **payload}
        rows.append(entry)
    else:
        entry.update(payload)
    return entry


def report_audit(message: str, actor: str) -> None:
    rows = store.rows("query_audit")
    rows.append({
        "id": next_id("query_audit"),
        "记录编号": f"AUDIT-{hashlib.md5(message.encode()).hexdigest()[:8]}",
        "查询语句": message,
        "执行耗时": "0ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": "experiment_shared_metric",
        "操作用户": actor,
        "查询时间": now(),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    })
