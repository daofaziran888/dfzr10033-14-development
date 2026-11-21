<template>
  <section class="page" data-module="experiment-shared-report">
    <header class="page-head">
      <div>
        <h2>实验共享报告</h2>
        <p class="page-desc">
          按实验归属读取可共享的分组指标。分母、转化量与对比值都按当前角色实际可见的曝光范围复算，
          受限分组不会以全量合计或小计形式出现。
        </p>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="load">
      <label>
        实验编号
        <input v-model="experimentId" data-testid="report-experiment" placeholder="如 AB_EXP_0002" />
      </label>
      <label>
        读取角色
        <select v-model="role" data-testid="report-role">
          <option value="analyst">analyst（公开组）</option>
          <option value="analyst_restricted">analyst_restricted（含受限组）</option>
          <option value="experiment_owner">experiment_owner（可发布）</option>
        </select>
      </label>
      <label>
        指标
        <input v-model="metric" data-testid="report-metric" />
      </label>
      <button class="btn primary" data-testid="report-load" type="submit">读取分组</button>
      <button class="btn" data-testid="report-generate" type="button" :disabled="!view" @click="generate">
        生成只读报告
      </button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="report-error">{{ errorMessage }}</p>
    <p v-if="notice" class="status-legend" data-testid="report-notice">{{ notice }}</p>

    <template v-if="view">
      <div class="stat-row">
        <article v-for="card in cards" :key="card.label" class="stat-card">
          <span class="stat-label">{{ card.label }}</span>
          <strong class="stat-value">{{ card.value }}</strong>
        </article>
      </div>

      <table class="data-table" data-testid="report-groups">
        <thead>
          <tr>
            <th>分组</th><th>类型</th><th>分桶版本</th><th>样本量</th><th>转化量</th><th>转化率</th><th>共享范围</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="group in view.groups" :key="group.分组名称">
            <td>{{ group.分组名称 }}</td>
            <td>{{ group.分组类型 || '—' }}</td>
            <td>{{ group.分桶版本 }}</td>
            <td>{{ group.样本量 }}</td>
            <td>{{ group.转化量 }}</td>
            <td>{{ group.转化率 === null ? '—' : (group.转化率 * 100).toFixed(2) + '%' }}</td>
            <td>{{ group.共享范围 }}</td>
          </tr>
          <tr v-if="!view.groups.length">
            <td colspan="7" class="empty-state">当前角色没有可读分组</td>
          </tr>
        </tbody>
      </table>

      <section class="analysis-panel" data-testid="report-suppressed">
        <h3>口径说明</h3>
        <p v-if="view.total === null">全量合计：<strong>已抑制</strong>（不能与可见组小计相减反推受限组）</p>
        <p v-else>
          全量合计：样本量 {{ view.total.样本量 }}，转化量 {{ view.total.转化量 }}
        </p>
        <ul v-if="view.suppressed.length">
          <li v-for="item in view.suppressed" :key="item">{{ item }}</li>
        </ul>
        <p v-if="view.hidden.length" class="error-text">
          受限分组：{{ view.hidden.map((item) => item.分组名称).join('、') }}
          （仅显示名称，不返回样本量与转化量）
        </p>
      </section>

      <section v-if="report" class="analysis-panel" data-testid="report-draft">
        <h3>只读报告 {{ report.报告编号 }}</h3>
        <p>视图范围：{{ report.视图范围 }}；分桶版本：{{ report.分桶版本 }}</p>
        <p>对比值：{{ contrastText }}</p>
        <button class="btn primary" data-testid="report-publish" type="button" @click="publish">
          按可见组发布
        </button>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { loadSharedExperiment, openSharedReport, publishSharedReport } from '@/api/backend-service'
import type { SharedExperimentView } from '@/api/backend-service'

const experimentId = ref('AB_EXP_0002')
const role = ref('analyst')
const metric = ref('转化')
const view = ref<SharedExperimentView | null>(null)
type ReportRow = {
  报告编号: string
  视图范围: string
  分桶版本: string
  对比: Record<string, unknown>[]
  [field: string]: unknown
}
const report = ref<ReportRow | null>(null)
const errorMessage = ref('')
const notice = ref('')

const contrastText = computed(() => {
  const rows = report.value?.对比 ?? []
  return rows.length ? JSON.stringify(rows) : '未生成（授权范围不满足成对分析）'
})

const cards = computed(() => {
  if (!view.value) return []
  return [
    { label: '可读分组', value: view.value.groups.length },
    { label: '分桶版本', value: view.value.version },
    { label: '全量合计', value: view.value.total ? '已授权' : '已抑制' },
    { label: '受限分组', value: view.value.hidden.length },
  ]
})

async function load() {
  errorMessage.value = ''
  notice.value = ''
  report.value = null
  try {
    view.value = await loadSharedExperiment(experimentId.value.trim(), role.value, metric.value.trim() || '转化')
  } catch (error) {
    view.value = null
    errorMessage.value = error instanceof Error ? error.message : '读取失败'
  }
}

async function generate() {
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await openSharedReport(experimentId.value.trim(), role.value, metric.value.trim() || '转化')
    report.value = payload.report as ReportRow
    notice.value = '共享报告草稿已生成，发布时会按当时授权范围再核对一次。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成失败'
  }
}

async function publish() {
  if (!report.value) return
  errorMessage.value = ''
  notice.value = ''
  try {
    const payload = await publishSharedReport(
      experimentId.value.trim(),
      String(report.value.报告编号),
      true,
    )
    report.value = payload.report as ReportRow
    notice.value = '已按实际授权范围发布，发布清单已更新。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布失败'
  }
}
</script>
