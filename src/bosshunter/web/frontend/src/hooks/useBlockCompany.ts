import { useCallback, useEffect, useRef, useState } from 'react'
import type { Job } from '@/hooks/useDashboard'
import { findBlockingRule } from '@/lib/blockedCompanies'

export const CONFIG_SAVED_EVENT = 'bosshunter-config-saved'

async function readBlockedCompanies(): Promise<string[]> {
  const res = await fetch('/api/agent/state', { cache: 'no-store' })
  const data = await res.json().catch(() => ({}))
  const rules = data.preferences?.blocked_companies
  if (!res.ok || !Array.isArray(rules) || !rules.every(rule => typeof rule === 'string')) {
    throw new Error(data.error || '读取屏蔽名单失败，请重试。')
  }
  return rules as string[]
}

async function postConfig(path: string, payload: Record<string, unknown>) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    const blocked = Array.isArray(data.blocked)
      ? data.blocked.map((item: { job_id?: string; reasons?: string[] }) => `${item.job_id || '岗位'}：${(item.reasons || []).join('、')}`).join('；')
      : ''
    throw new Error([data.error || '岗位操作失败', blocked].filter(Boolean).join('；'))
  }
  return res.json()
}

export function useBlockCompany(onNotice: (message: string) => void, enabled = true) {
  const [blockedCompanies, setBlockedCompanies] = useState<string[]>([])
  const [blockingJobId, setBlockingJobId] = useState<string | null>(null)
  const [blockNotice, setBlockNotice] = useState<{ jobId: string; message: string } | null>(null)
  const blockingRef = useRef(false)

  useEffect(() => {
    if (!enabled) return
    const load = () => {
      readBlockedCompanies()
        .then(setBlockedCompanies)
        .catch(() => { /* 点击屏蔽时会重新读取，并显示读取错误。 */ })
    }
    load()
    window.addEventListener(CONFIG_SAVED_EVENT, load)
    return () => window.removeEventListener(CONFIG_SAVED_EVENT, load)
  }, [enabled])

  const blockCompany = useCallback(async (job: Job) => {
    const company = (job.company || '').trim()
    if (!company || blockingRef.current) return
    blockingRef.current = true
    setBlockingJobId(job.id)
    setBlockNotice(null)
    // 卡片内就近提示：页面顶部的通用提示在长列表里常常滚出视野，看起来像"没反应"。
    const report = (message: string) => {
      setBlockNotice({ jobId: job.id, message })
      onNotice(message)
    }
    report('正在读取屏蔽名单并准备配置预览…')
    try {
      const current = await readBlockedCompanies()
      setBlockedCompanies(current)
      const existing = findBlockingRule(company, current)
      if (existing) {
        window.dispatchEvent(new Event(CONFIG_SAVED_EVENT))
        report(`“${company}”已被“${existing}”屏蔽。可在配置 → 个人信息 → 屏蔽公司中解除。`)
        return
      }
      const preferences = { blocked_companies: [...current, company] }
      await postConfig('/api/agent/config/preview', { preferences })
      // 预览到保存期间可能在其他页面修改了名单，避免用旧预览覆盖新规则。
      const latest = await readBlockedCompanies()
      if (JSON.stringify(latest) !== JSON.stringify(current)) {
        setBlockedCompanies(latest)
        throw new Error('屏蔽名单已变化，请重新点击“屏蔽公司”查看最新预览。')
      }
      const result = await postConfig('/api/agent/config/apply', { preferences, confirm: true })
      const saved = result?.preferences?.blocked_companies
      setBlockedCompanies(Array.isArray(saved) ? saved : preferences.blocked_companies)
      window.dispatchEvent(new Event(CONFIG_SAVED_EVENT))
      report(`已屏蔽“${company}”，匹配岗位已从列表隐藏，记录保留；解除屏蔽后恢复显示。`)
    } catch (cause) {
      report(cause instanceof Error ? cause.message : '屏蔽公司失败，请重试。')
    } finally {
      blockingRef.current = false
      setBlockingJobId(null)
    }
  }, [onNotice])

  return { blockedCompanies, blockingJobId, blockNotice, blockCompany }
}
