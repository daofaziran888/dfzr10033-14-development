"""可共享实验分组指标领域服务。

把实验分桶配置、曝光记录和指标明细关联起来，聚合成按实验归属共享的分组指标；
报告读者只能读取自己角色授权范围内的分组，且所有分母、对比值都在读取时按
实际授权曝光范围复算。
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

from app.services import experiment_common as ec
from app.services.base import BaseModuleService
from app.store import store


class ExpGroupMetricService(BaseModuleService):
    MODULE = "exp_group_metric"
    ENTITY = "实验分组指标"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "分组指标编号", "实验编号", "分桶版本", "分组名称", "指标名称",
        "样本量", "转化量", "转化率", "曝光范围", "共享范围",
    ]
    REQUIRED_FIELDS = ["实验编号", "分组名称", "指标名称"]
    STATUS_ORDER = ["待聚合", "已聚合", "已失效", "已归档"]
    ACTION_RULES = {"确认聚合": "已聚合", "标记失效": "已失效", "归档指标": "已归档"}
    NEGATIVE_ACTIONS = ["标记失效", "归档指标"]

    # -- 曝光写入：分桶版本随曝光锁定，同一条曝光只有一个实际组 --

    def record_exposure(
        self,
        experiment_id: str,
        user_id: str,
        *,
        exposure_code: str | None = None,
        version: str | None = None,
        sample: int = 1,
        conversions: int = 0,
        metric: str | None = None,
        sample_size: int | None = None,
        converted: int = 0,
    ) -> dict[str, Any]:
        """写入曝光并锁定写入时的分桶版本；同一曝光编号/同一用户同版本只归一个组。"""
        if not experiment_id or not user_id:
            return {"ok": False, "message": "实验编号和用户标识不能为空"}
        version = version or ec.latest_version(experiment_id)
        if not version:
            return {"ok": False, "message": "实验没有可用的分桶版本，请先登记分桶配置"}

        group = ec.resolve_group(experiment_id, user_id, version)
        if not group:
            return {"ok": False, "message": f"分桶版本 {version} 没有可用分组"}

        code = exposure_code or f"EXP-{experiment_id}-{version}-{uuid.uuid4().hex[:10]}"
        exposures = store.rows(ec.EXPOSURE_TABLE)

        # 同一曝光编号重复写入时按幂等处理，且不允许改到别的组。
        for row in exposures:
            if str(row.get("曝光编号", "")) == code:
                if str(row.get("分组名称", "")) != group:
                    return {
                        "ok": False,
                        "message": (
                            f"曝光 {code} 已锁定在分组 {row.get('分组名称')}"
                            f"（分桶版本 {row.get('分桶版本')}），不能改归 {group}"
                        ),
                        "exposure": row,
                    }
                return {"ok": True, "message": "曝光已存在，沿用原锁定归属", "exposure": row}

        # 同一用户在同一分桶版本里只产生一条曝光，避免重复计入分母。
        for row in exposures:
            if (
                str(row.get("实验编号", "")) == experiment_id
                and str(row.get("用户标识", "")) == str(user_id)
                and str(row.get("分桶版本", "")) == version
            ):
                if str(row.get("分组名称", "")) != group:
                    return {
                        "ok": False,
                        "message": (
                            f"用户 {user_id} 在分桶版本 {version} 已归属"
                            f"{row.get('分组名称')}，不能重复归到 {group}"
                        ),
                        "exposure": row,
                    }
                return {"ok": True, "message": "该用户在本版本已有曝光，沿用原归属", "exposure": row}

        share = ec.share_of(experiment_id, group)
        entry = {
            "id": ec.next_id(ec.EXPOSURE_TABLE),
            "曝光编号": code,
            "实验编号": experiment_id,
            "用户标识": str(user_id),
            "分组名称": group,
            "分桶版本": version,
            "锁定版本": version,
            "共享范围": share,
            "曝光时间": ec.now(),
            "样本数": str(max(int(sample_size if sample_size is not None else sample), 0)),
            "转化数": str(max(int(converted), 0)),
            "status": "已曝光",
            "pending": False,
            "abnormal": False,
        }
        exposures.append(entry)

        metric_name = metric or "转化"
        details = store.rows(ec.DETAIL_TABLE)
        details.append({
            "id": ec.next_id(ec.DETAIL_TABLE),
            "实验编号": experiment_id,
            "指标名称": metric_name,
            "曝光编号": code,
            "分桶版本": version,
            "样本数": entry["样本数"],
            "转化数": entry["转化数"] if conversions == 0 else str(max(int(conversions), 0)),
            "记录时间": ec.now(),
            "status": "已记录",
            "pending": False,
            "abnormal": False,
        })
        return {"ok": True, "message": f"曝光已归入 {group}", "exposure": entry, "group": group}

    def record_metric_detail(
        self,
        experiment_id: str,
        exposure_code: str,
        metric: str,
        *,
        sample_size: int,
        conversions: int,
    ) -> dict[str, Any]:
        """补充指标明细；曝光必须已存在且归属已锁定。"""
        exposure = next(
            (row for row in store.rows(ec.EXPOSURE_TABLE)
             if str(row.get("曝光编号", "")) == str(exposure_code)
             and str(row.get("实验编号", "")) == experiment_id),
            None,
        )
        if exposure is None:
            return {"ok": False, "message": "曝光记录不存在，指标明细不能脱离曝光写入"}
        row = {
            "id": ec.next_id(ec.DETAIL_TABLE),
            "实验编号": experiment_id,
            "指标名称": metric,
            "曝光编号": str(exposure_code),
            "分桶版本": str(exposure.get("分桶版本", "")),
            "样本数": str(max(int(sample_size), 0)),
            "转化数": str(max(int(conversions), 0)),
            "记录时间": ec.now(),
            "status": "已记录",
            "pending": False,
            "abnormal": False,
        }
        store.rows(ec.DETAIL_TABLE).append(row)
        return {"ok": True, "message": "指标明细已写入", "detail": row}

    # -- 按角色读取共享分组指标 --

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        role: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """列表接口按最小权限返回：默认角色只能看到公开分组。

        真实部署里 role 必须由登录态注入；这里显式保留参数，避免通用 CRUD
        列表把受限分组的样本量、转化量直接透出。
        """
        profile = ec.role_profile(role)
        allowed = set(profile["scopes"])
        rows: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            share = ec.normalize_share_scope(row.get("共享范围", ec.RESTRICTED))
            if share == ec.EXITED or share not in allowed:
                continue
            if profile["groups"] and str(row.get("分组名称", "")) not in profile["groups"]:
                continue
            rows.append(row)
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """单条读取遵循最小权限：受限组对默认读者按不存在处理，避免绕过列表口径。"""
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None
        share = ec.normalize_share_scope(row.get("共享范围", ec.RESTRICTED))
        if share == ec.EXITED or share not in ec.role_profile(None)["scopes"]:
            return None
        return row

    def shared_group_metrics(
        self,
        experiment_id: str,
        role: str | None = None,
        metric: str = "转化",
        version: str | None = None,
    ) -> dict[str, Any]:
        """读取角色可访问的分组；不可见组不仅不出数，也不能通过合计反推。"""
        view = self._authorized_view(experiment_id, role, metric, version)
        if not view.get("ok"):
            return view
        # 不返回未授权组的任何数值，只保留“存在未授权组”的事实和名称。
        return {
            "ok": True,
            "experiment_id": view["experiment_id"],
            "version": view["version"],
            "metric": view["metric"],
            "role": view["role"],
            "scopes": view["scopes"],
            "groups": view["groups"],
            "contrasts": view["contrasts"],
            "total": view["total"],
            "suppressed": view["suppressed"],
            "hidden": view["hidden"],
            "has_hidden": view["has_hidden"],
        }

    def compute_group_metrics(
        self,
        experiment_id: str,
        metric: str = "转化",
        version: str | None = None,
        *,
        actor_role: str | None = "metric_engine",
    ) -> dict[str, Any]:
        """按分桶版本和分组聚合，保存该组明确的曝光编号范围。

        这是内部计算动作，会看到全部组；调用方必须持有可发布/受限范围权限。
        """
        profile = ec.role_profile(actor_role)
        if not profile["can_publish"] and ec.RESTRICTED not in profile["scopes"]:
            return {"ok": False, "message": f"角色 {profile['role']} 无权生成全量分组指标"}
        version = version or ec.latest_version(experiment_id)
        if not version:
            return {"ok": False, "message": "实验没有可用的分桶版本"}
        configs = ec.group_configs(experiment_id, version)
        if not configs:
            return {"ok": False, "message": f"分桶版本 {version} 没有分组配置"}

        shares = ec.current_shares(experiment_id)
        exposures = ec.exposures_for(experiment_id, version)
        seen: dict[str, str] = {}
        for exposure in exposures:
            group = str(exposure.get("分组名称", ""))
            if group not in configs:
                return {
                    "ok": False,
                    "message": f"曝光 {exposure.get('曝光编号')} 归属的 {group} 不在分桶版本 {version}",
                }
            code = str(exposure.get("曝光编号", ""))
            if code in seen and seen[code] != group:
                return {"ok": False, "message": f"曝光 {code} 同时归入多个分组，拒绝聚合"}
            seen[code] = group

        rows: list[dict[str, Any]] = []
        for group in configs:
            codes = [str(row.get("曝光编号", "")) for row in exposures
                     if str(row.get("分组名称", "")) == group]
            values = ec.aggregate_scope(experiment_id, metric, codes)
            values["样本量"] = int(values.get("样本量", 0))
            values["转化量"] = int(values.get("转化量", 0))
            entry = self._upsert_group_metric(
                experiment_id, version, group, metric, values,
                share=shares.get(group, ec.RESTRICTED),
                group_kind=str(configs[group].get("分组类型", "")),
            )
            rows.append(entry)

        return {
            "ok": True,
            "experiment_id": experiment_id,
            "version": version,
            "metric": metric,
            "groups": rows,
            "exposure_count": len(exposures),
        }

    def _upsert_group_metric(
        self,
        experiment_id: str,
        version: str,
        group: str,
        metric: str,
        values: dict[str, Any],
        *,
        share: str,
        group_kind: str,
    ) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        entry = next(
            (row for row in rows
             if str(row.get("实验编号", "")) == experiment_id
             and str(row.get("分桶版本", "")) == version
             and str(row.get("分组名称", "")) == group
             and str(row.get("指标名称", "")) == metric),
            None,
        )
        payload = {
            "分组指标编号": f"EGM-{experiment_id}-{version}-{group}",
            "实验编号": experiment_id,
            "分桶版本": version,
            "分组名称": group,
            "分组类型": group_kind,
            "指标名称": metric,
            "样本量": str(values.get("样本量", 0)),
            "转化量": str(values.get("转化量", 0)),
            "转化率": "" if values.get("转化率") is None else f"{values['转化率']:.6f}",
            "曝光范围": values.get("曝光范围", ""),
            "曝光数": str(values.get("曝光数", 0)),
            "共享范围": share,
            "计算时间": ec.now(),
        }
        if entry is None:
            entry = {
                "id": ec.next_id(self.MODULE),
                "status": "已聚合",
                "pending": False,
                "abnormal": False,
                **payload,
            }
            rows.append(entry)
        else:
            entry.update(payload)
            entry["status"] = "已聚合"
            entry["pending"] = False
        return entry

    @staticmethod
    def _public_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "分组名称": row.get("分组名称"),
            "分组类型": row.get("分组类型"),
            "分桶版本": row.get("分桶版本"),
            "指标名称": row.get("指标名称"),
            "样本量": int(row.get("样本量", 0)),
            "转化量": int(row.get("转化量", 0)),
            "转化率": ec.safe_rate(int(row.get("转化量", 0)), int(row.get("样本量", 0))),
            "曝光数": int(row.get("曝光数", 0)),
            "曝光范围": row.get("曝光范围", ""),
            "共享范围": row.get("共享范围"),
        }

    @staticmethod
    def _public_aggregate(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "样本量": int(values.get("样本量", 0)),
            "转化量": int(values.get("转化量", 0)),
            "转化率": values.get("转化率"),
            "曝光数": int(values.get("曝光数", 0)),
            "分组数": 0,
        }

    # -- 共享范围调整 --

    def set_group_share(
        self,
        experiment_id: str,
        group: str,
        share: str,
        *,
        actor: str = "experiment_owner",
        version: str | None = None,
    ) -> dict[str, Any]:
        """调整分组的共享归属；不影响已交付报告的历史快照，但新报告立即按新状态复算。"""
        version = version or ec.latest_version(experiment_id)
        configs = ec.group_configs(experiment_id, version)
        if configs and group not in configs:
            return {"ok": False, "message": f"分组 {group} 不在分桶版本 {version}"}
        target = ec.normalize_share_scope(share)
        if target not in ec.SHARE_SCOPES:
            return {"ok": False, "message": f"未知共享范围：{share}"}
        before = ec.share_of(experiment_id, group)

        rows = store.rows(ec.SHARE_TABLE)
        row = next(
            (item for item in rows
             if str(item.get("实验编号", "")) == experiment_id
             and str(item.get("分组名称", "")) == group),
            None,
        )
        payload = {
            "实验编号": experiment_id,
            "分组名称": group,
            "共享范围": target,
            "生效版本": version or "",
            "更新时间": ec.now(),
            "操作人": actor,
        }
        if row is None:
            row = {
                "id": ec.next_id(ec.SHARE_TABLE),
                "status": target,
                "pending": False,
                "abnormal": False,
                **payload,
            }
            rows.append(row)
        else:
            row.update(payload)
            row["status"] = target

        # 分组指标上的共享标记同步刷新，但历史报告和交付记录不改。
        for metric_row in store.rows(self.MODULE):
            if (
                str(metric_row.get("实验编号", "")) == experiment_id
                and str(metric_row.get("分组名称", "")) == group
            ):
                metric_row["共享范围"] = target

        ec.report_audit(
            f"GROUP_SHARE_ADJUST experiment={experiment_id} group={group} "
            f"from={before} to={target} version={version}",
            actor,
        )
        affected = [
            {
                "报告编号": report.get("报告编号"),
                "角色": report.get("角色"),
                "status": report.get("status"),
                "发布时间": report.get("发布时间"),
            }
            for report in store.rows(ec.REPORT_TABLE)
            if str(report.get("实验编号", "")) == experiment_id
            and str(report.get("status", "")) in ("草稿", "待复核", "已发布")
        ]

        # 已有读者交付记录不删除，但退出共享或收紧为受限时标记回收，
        # 便于核对哪些历史交付仍持有受限组的样本量与转化量。
        recalled: list[dict[str, Any]] = []
        if target == ec.EXITED or (before == ec.PUBLIC and target == ec.RESTRICTED):
            for delivery in store.rows(ec.DELIVERY_TABLE):
                if str(delivery.get("实验编号", "")) != experiment_id:
                    continue
                visible = ec.split_groups(delivery.get("可见分组", ""))
                if group in visible and delivery.get("status") != "已回收":
                    delivery["status"] = "待回收"
                    delivery["abnormal"] = True
                    delivery["回收原因"] = f"分组 {group} 共享范围调整为 {target}"
                    delivery["回收时间"] = ec.now()
                    recalled.append({
                        "交付编号": delivery.get("交付编号"),
                        "报告编号": delivery.get("报告编号"),
                        "角色": delivery.get("角色"),
                        "状态": delivery.get("status"),
                    })
        return {
            "ok": True,
            "message": f"分组 {group} 共享范围已从 {before} 调整为 {target}",
            "experiment_id": experiment_id,
            "group": group,
            "before": before,
            "after": target,
            "version": version,
            "affected_reports": affected,
            "deliveries": self.list_deliveries(experiment_id=experiment_id),
            "recalled_deliveries": recalled,
        }

    # -- 共享只读报告 --

    def prepare_shared_report(
        self,
        experiment_id: str,
        role: str | None = None,
        *,
        metric: str = "转化",
        version: str | None = None,
        scope: str = "全部",
        created_by: str = "experiment_owner",
    ) -> dict[str, Any]:
        """生成共享报告草稿；受限组存在时，报告只含授权范围并标明抑制项。"""
        view = self._authorized_view(experiment_id, role, metric, version)
        if not view.get("ok"):
            return view
        report_id = f"ABR-{experiment_id}-{uuid.uuid4().hex[:8]}"
        report = self._report_payload(
            report_id, view, scope=scope, created_by=created_by, status="草稿",
        )
        report["status"] = "草稿"
        report["发布冲突"] = None
        rows = store.rows(ec.REPORT_TABLE)
        entry = {
            "id": ec.next_id(ec.REPORT_TABLE),
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            **report,
        }
        rows.append(entry)
        return {"ok": True, "message": "共享报告草稿已生成", "report": entry}

    def publish_report(
        self,
        experiment_id: str,
        report_id: str | int,
        *,
        actor: str = "experiment_owner",
        publish_mode: str = "paired",
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        """发布共享报告：发布前重新核对授权范围，冲突时挂起而不是悄悄缩小口径。"""
        actor_profile = ec.role_profile(actor)
        if not actor_profile["can_publish"]:
            return {"ok": False, "message": f"角色 {actor_profile['role']} 无权发布共享报告"}
        report = next(
            (row for row in store.rows(ec.REPORT_TABLE)
             if str(row.get("实验编号", "")) == experiment_id
             and str(row.get("报告编号", "")) == str(report_id)),
            None,
        )
        if report is None:
            return {"ok": False, "message": "共享报告不存在"}
        if report.get("status") == "已发布":
            # 已发布的报告是读者已收到的历史快照，不能沿用同一编号改写口径。
            # 需要新口径时重新生成草稿，交付记录随新报告单独留痕。
            return {
                "ok": False,
                "message": "报告已发布且读者已收到，不能就地改写；请重新生成报告后再发布",
                "report": report,
            }

        role = report.get("角色")
        metric = str(report.get("指标名称", "转化"))
        version = str(report.get("分桶版本", ""))
        access = ec.authorized_groups(experiment_id, version, role)
        view = self._authorized_view(experiment_id, role, metric, version)
        if not view.get("ok"):
            return view

        pair_gap = self._pair_gap(view)
        conflict = ""

        # 计算草稿时报告里可能仍含有发布时才退出共享的组；只要该组此刻已不可读，
        # 就不能把草稿里的旧数值原样发布。冲突时保留待复核快照，不写交付记录。
        stale_visible = {
            str(row.get("分组名称", ""))
            for row in report.get("分组结果", [])
        } - set(access["groups"])
        # 成对分析需要对照组与实验组同时可读；草稿里含有此刻已退出共享的组，
        # 正是发布阶段最典型的“准备发布时一组退出共享”冲突。
        if pair_gap and not allow_partial:
            conflict = (
                "成对分析需要对照组与实验组同时可读，但当前有分组不在共享范围内("
                + "、".join(item["分组名称"] for item in access["hidden"])
                + ")。请选择只公布可见组，或恢复该组共享后再发布。"
            )
        elif stale_visible and not allow_partial:
            conflict = (
                "报告草稿包含此刻已不在共享范围内的分组："
                + "、".join(sorted(stale_visible))
                + "。必须按当前授权范围重算后才能发布。"
            )
        elif view["has_hidden"] and publish_mode == "paired" and not allow_partial:
            conflict = (
                "成对分析需要对照组与实验组同时可读，但当前有分组不在共享范围内："
                + "、".join(item["分组名称"] for item in access["hidden"])
                + "。请选择只公布可见组，或恢复该组共享后再发布。"
            )

        if conflict:
            report.update(self._report_payload(
                str(report.get("报告编号")), view,
                scope=str(report.get("视图范围", "全部")),
                created_by=str(report.get("创建人", actor)),
                status="待复核",
            ))
            report["status"] = "待复核"
            report["待复核"] = True
            report["发布冲突"] = conflict
            report["pending"] = True
            report["abnormal"] = True
            ec.report_audit(
                f"REPORT_PUBLISH_CONFLICT experiment={experiment_id} report={report.get('报告编号')} "
                f"role={role} reason={conflict}",
                actor,
            )
            return {
                "ok": False,
                "conflict": True,
                "message": conflict,
                "report": report,
                "hidden": access["hidden"],
            }

        # 显式选择只公布可见组时，重新按实际授权范围生成被抑制的报告；
        # 原来草稿里的旧数值不会带入，分组结果直接来自当前授权的复算结果。
        report.update(self._report_payload(
            str(report.get("报告编号")), view,
            scope="可见组" if (view["has_hidden"] or pair_gap) else "全部",
            created_by=str(report.get("创建人", actor)),
            status="已发布",
        ))
        report["status"] = "已发布"
        report["待复核"] = False
        report["发布冲突"] = None
        report["pending"] = False
        report["abnormal"] = False
        report["发布时间"] = ec.now()
        report["发布人"] = actor
        report["发布方式"] = "仅可见组" if report["视图范围"] == "可见组" else "完整授权范围"

        delivery = self._write_delivery(report, actor)
        self._append_release(report)
        ec.report_audit(
            f"REPORT_PUBLISH experiment={experiment_id} report={report.get('报告编号')} "
            f"role={role} scope={report['视图范围']} groups={len(report.get('分组结果', []))}",
            actor,
        )
        return {
            "ok": True,
            "message": "共享报告已按实际授权范围发布",
            "report": report,
            "delivery": delivery,
        }

    def list_reports(self, experiment_id: str | None = None, role: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ec.REPORT_TABLE)
        if experiment_id:
            rows = [row for row in rows if str(row.get("实验编号", "")) == experiment_id]
        if role:
            rows = [row for row in rows if str(row.get("角色", "")) == role]
        return list(rows)

    def list_deliveries(
        self,
        experiment_id: str | None = None,
        report_id: str | None = None,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(ec.DELIVERY_TABLE)
        if experiment_id:
            rows = [row for row in rows if str(row.get("实验编号", "")) == experiment_id]
        if report_id:
            rows = [row for row in rows if str(row.get("报告编号", "")) == report_id]
        if role:
            rows = [row for row in rows if str(row.get("角色", "")) == role]
        return list(rows)

    def list_releases(self, experiment_id: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ec.RELEASE_TABLE)
        if experiment_id:
            rows = [row for row in rows if str(row.get("实验编号", "")) == experiment_id]
        return list(rows)

    # -- 内部组装 --

    def _authorized_view(
        self,
        experiment_id: str,
        role: str | None,
        metric: str,
        version: str | None,
    ) -> dict[str, Any]:
        version = version or ec.latest_version(experiment_id)
        if not version:
            return {"ok": False, "message": "实验没有可用的分桶版本"}
        access = ec.authorized_groups(experiment_id, version, role)
        visible = access["groups"]
        all_groups = ec.group_configs(experiment_id, version)
        if not visible:
            return {
                "ok": False,
                "message": f"角色 {access['role']} 没有任何可读分组",
                "hidden": access["hidden"],
            }

        stored = {
            str(row.get("分组名称", "")): row
            for row in store.rows(self.MODULE)
            if str(row.get("实验编号", "")) == experiment_id
            and str(row.get("分桶版本", "")) == version
            and str(row.get("指标名称", "")) == metric
        }
        missing_groups = [name for name in visible if name not in stored]
        if missing_groups:
            # 只对缺失的分组做一次聚合，已有分组指标保持原样，
            # 这样“指标与曝光范围是否一致”仍能被下游校验出来。
            refreshed = self.compute_group_metrics(
                experiment_id, metric, version, actor_role="metric_engine",
            )
            if not refreshed.get("ok"):
                return refreshed
            stored = {
                str(row.get("分组名称", "")): row
                for row in store.rows(self.MODULE)
                if str(row.get("实验编号", "")) == experiment_id
                and str(row.get("分桶版本", "")) == version
                and str(row.get("指标名称", "")) == metric
            }

        # 每个可见组只取分组指标上登记的曝光范围，并再按曝光归属校验一次；
        # 分母、转化量都在这个范围内重算，不使用任何全量合计。
        group_rows: list[dict[str, Any]] = []
        group_order = [name for name in all_groups if name in visible]
        for group in visible:
            metric_row = stored.get(group, {})
            declared = ec.split_groups(metric_row.get("曝光范围", ""))
            allowed_codes = {
                str(row.get("曝光编号", ""))
                for row in ec.exposures_for(experiment_id, version, [group])
            }
            # 声明范围之外的曝光不能被别的组“借用”，否则视为口径异常。
            borrowed = sorted(set(declared) - allowed_codes)
            if borrowed:
                return {
                    "ok": False,
                    "message": f"分组 {group} 登记的曝光不在该组实际曝光内：{borrowed}",
                }
            codes = [code for code in declared if code in allowed_codes]
            values = ec.aggregate_scope(experiment_id, metric, codes)
            row = self._public_row({
                "分组名称": group,
                "分组类型": ec.group_type(experiment_id, group, version),
                "分桶版本": version,
                "指标名称": metric,
                "样本量": values["样本量"],
                "转化量": values["转化量"],
                "转化率": values["转化率"],
                "曝光数": values["曝光数"],
                "曝光范围": values["曝光范围"],
                "共享范围": ec.share_of(experiment_id, group),
            })
            row["口径校验"] = (
                "一致"
                if int(metric_row.get("样本量", 0)) == values["样本量"]
                and int(metric_row.get("转化量", 0)) == values["转化量"]
                else "已按实际曝光范围复算"
            )
            group_rows.append(row)
        group_rows.sort(key=lambda item: group_order.index(item["分组名称"]) if item["分组名称"] in group_order else 99)

        contrasts: list[dict[str, Any]] = []
        pair_gap = False
        control = next(
            (row for row in group_rows if row.get("分组类型") == "对照组"),
            None,
        )
        treatment = next(
            (row for row in group_rows if row.get("分组类型") == "实验组"),
            None,
        )
        all_controls = [name for name, cfg in all_groups.items() if cfg.get("分组类型") == "对照组"]
        all_treatments = [name for name, cfg in all_groups.items() if cfg.get("分组类型") == "实验组"]
        if control and treatment:
            contrasts.append(self._contrast(control, treatment))
        elif all_controls and all_treatments:
            pair_gap = True

        total = None
        suppressed: list[str] = []
        hidden_names = [item["分组名称"] for item in access["hidden"]]
        if access["has_hidden"]:
            suppressed.append(
                "全量合计：存在未授权分组 " + "、".join(hidden_names)
                + "，公布合计减去可见组小计可反推受限组"
            )
            suppressed.append("受限组小计、样本量与转化量：未获得该组共享授权")
            if pair_gap:
                suppressed.append("成对分析：对照组或实验组不在当前授权范围")
        elif pair_gap:
            suppressed.append("成对分析：对照组与实验组不完整")

        if not access["has_hidden"]:
            codes = [
                str(row.get("曝光编号", ""))
                for row in ec.exposures_for(experiment_id, version, visible)
            ]
            aggregated = ec.aggregate_scope(experiment_id, metric, codes)
            total = {
                "样本量": aggregated["样本量"],
                "转化量": aggregated["转化量"],
                "转化率": aggregated["转化率"],
                "曝光数": aggregated["曝光数"],
                "分组数": len(group_rows),
            }

        return {
            "ok": True,
            "experiment_id": experiment_id,
            "version": version,
            "metric": metric,
            "role": access["role"],
            "scopes": access["scopes"],
            "groups": group_rows,
            "contrasts": contrasts,
            "total": total,
            "suppressed": suppressed,
            "hidden": access["hidden"],
            "has_hidden": access["has_hidden"],
            "pair_gap": pair_gap,
        }

    @staticmethod
    def _pair_gap(view: dict[str, Any]) -> bool:
        if view.get("pair_gap"):
            return True
        groups = view.get("groups", [])
        kinds = {str(row.get("分组类型", "")) for row in groups}
        return bool(groups) and not ({"对照组", "实验组"} <= kinds)

    @staticmethod
    def _contrast(control: dict[str, Any], treatment: dict[str, Any]) -> dict[str, Any]:
        control_rate = control.get("转化率")
        treatment_rate = treatment.get("转化率")
        lift = None
        relative = None
        if control_rate is not None and treatment_rate is not None:
            lift = round(treatment_rate - control_rate, 6)
            if control_rate:
                relative = round((treatment_rate - control_rate) / control_rate, 6)
        return {
            "对照组": control.get("分组名称"),
            "实验组": treatment.get("分组名称"),
            "对照组转化率": control_rate,
            "实验组转化率": treatment_rate,
            "绝对提升": lift,
            "相对提升": relative,
            "对照组样本量": control.get("样本量"),
            "实验组样本量": treatment.get("样本量"),
        }

    @staticmethod
    def _report_payload(
        report_id: str,
        view: dict[str, Any],
        *,
        scope: str,
        created_by: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "报告编号": report_id,
            "实验编号": view["experiment_id"],
            "分桶版本": view["version"],
            "指标名称": view["metric"],
            "角色": view["role"],
            "角色可读范围": "、".join(view["scopes"]),
            "视图范围": scope if status != "草稿" else (scope or "全部"),
            "分组结果": view["groups"],
            "对比": view["contrasts"],
            "全量合计": view["total"],
            "抑制项": view["suppressed"],
            "受限分组": view["hidden"],
            "创建人": created_by,
            "生成时间": ec.now(),
            "复核状态": "需复核" if view["has_hidden"] or view.get("pair_gap") else "无冲突",
            "报告口径": "按实际授权曝光范围复算，分母为授权曝光样本量",
        }

    def _write_delivery(self, report: dict[str, Any], actor: str) -> dict[str, Any]:
        rows = store.rows(ec.DELIVERY_TABLE)
        delivery = {
            "id": ec.next_id(ec.DELIVERY_TABLE),
            "交付编号": f"DEL-{uuid.uuid4().hex[:8]}",
            "实验编号": report.get("实验编号"),
            "报告编号": report.get("报告编号"),
            "角色": report.get("角色"),
            "分桶版本": report.get("分桶版本"),
            "视图范围": report.get("视图范围"),
            "可见分组": "、".join(str(row.get("分组名称")) for row in report.get("分组结果", [])),
            "受限分组": "、".join(str(row.get("分组名称")) for row in report.get("受限分组", [])),
            "交付时间": ec.now(),
            "操作人": actor,
            "status": "已交付",
            "pending": False,
            "abnormal": False,
            "口径摘要": {
                "分组结果": report.get("分组结果", []),
                "对比": report.get("对比", []),
                "全量合计": report.get("全量合计"),
                "抑制项": report.get("抑制项", []),
            },
        }
        rows.append(delivery)
        return delivery

    def _append_release(self, report: dict[str, Any]) -> dict[str, Any]:
        return ec.write_release_entry(
            str(report.get("实验编号", "")), report, actor=str(report.get("发布人", "experiment_owner")),
        )
