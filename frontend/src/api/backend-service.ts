import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// -- 实验共享分组指标：按角色读取、发布共享报告 --

export type SharedGroup = {
  分组名称: string
  分组类型: string
  分桶版本: string
  指标名称: string
  样本量: number
  转化量: number
  转化率: number | null
  曝光数: number
  曝光范围: string
  共享范围: string
}

export type SharedExperimentView = {
  ok: boolean
  experiment_id: string
  version: string
  metric: string
  role: string
  groups: SharedGroup[]
  contrasts: Record<string, number | string | null>[]
  total: Record<string, number | null> | null
  suppressed: string[]
  hidden: { 分组名称: string; 原因: string }[]
  has_hidden: boolean
}

export async function loadSharedExperiment(
  experimentId: string,
  role: string,
  metric = '转化',
): Promise<SharedExperimentView> {
  const query = new URLSearchParams({ role, metric }).toString()
  const response = await request(`/api/experiments/${encodeURIComponent(experimentId)}/group-metrics?${query}`)
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? '共享分组指标读取失败')
  }
  return (await response.json()) as SharedExperimentView
}

export async function openSharedReport(
  experimentId: string,
  role: string,
  metric = '转化',
): Promise<{ report: Record<string, unknown> }> {
  const query = new URLSearchParams({ role, metric }).toString()
  const response = await request(`/api/experiments/${encodeURIComponent(experimentId)}/reports?${query}`, {
    method: 'POST',
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? '共享报告生成失败')
  }
  return await response.json()
}

export async function publishSharedReport(
  experimentId: string,
  reportId: string,
  allowPartial = false,
): Promise<Record<string, unknown>> {
  const query = new URLSearchParams({ allow_partial: String(allowPartial) }).toString()
  const response = await request(
    `/api/experiments/${encodeURIComponent(experimentId)}/reports/${encodeURIComponent(reportId)}/publish?${query}`,
    { method: 'POST' },
  )
  const payload = await response.json().catch(() => ({}))
  if (response.status === 409) {
    const detail = payload.detail ?? {}
    throw new Error(detail.message ?? '发布存在冲突，报告已转入待复核')
  }
  if (!response.ok) {
    throw new Error(payload.detail ?? payload.message ?? '共享报告发布失败')
  }
  return payload
}
