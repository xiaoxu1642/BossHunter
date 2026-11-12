import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import DashboardPage from './DashboardPage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('岗位列表屏蔽公司', () => {
  let rules: string[]
  let company: string
  let applyError: string
  let previewError: string
  let rulesAfterPreview: string[] | null
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/agent/state') {
      return jsonResponse({ preferences: { blocked_companies: [...rules] } })
    }
    if (url === '/api/agent/config/preview') {
      if (previewError) return jsonResponse({ error: previewError }, 400)
      const response = jsonResponse({ preferences: JSON.parse(String(init?.body)).preferences, requires_confirmation: true })
      if (rulesAfterPreview) rules = rulesAfterPreview
      return response
    }
    if (url === '/api/agent/config/apply') {
      if (applyError) return jsonResponse({ error: applyError }, 409)
      rules = JSON.parse(String(init?.body)).preferences.blocked_companies
      return jsonResponse({ success: true, preferences: { blocked_companies: rules } })
    }
    if (url.startsWith('/api/jobs/search')) {
      const blocked = rules.some(rule => rule.trim() && company.toLowerCase().includes(rule.trim().toLowerCase()))
      const items = blocked ? [] : [{ id: 'job-1', company, title: '前端工程师', status: 'ready', score: 90, created_at: '' }]
      return jsonResponse({
        items, total: items.length, all_total: items.length,
      })
    }
    if (url === '/api/workbench') {
      return jsonResponse({
        funnel: {}, funnel_today: {}, pending_confirmation: [], pending_greetings: [],
        send_errors: [], needs_resume: [], task: null, last_task: null,
        send_quota: { daily_limit: 30, sent: 0, remaining: 30, exhausted: false },
      })
    }
    return jsonResponse([])
  })

  const requestsTo = (path: string) => fetchMock.mock.calls.filter(([url]) => String(url) === path)

  beforeEach(() => {
    rules = ['已有屏蔽公司']
    company = '测试科技'
    applyError = ''
    previewError = ''
    rulesAfterPreview = null
    fetchMock.mockClear()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  async function clickBlock() {
    render(<DashboardPage view="jobs" />)
    const button = await screen.findByRole('button', { name: `屏蔽公司 ${company || '公司名缺失'}` })
    fireEvent.click(button)
    return button as HTMLButtonElement
  }

  it('saves directly after preview and changes only the company list while preserving existing rules', async () => {
    await clickBlock()
    expect(await screen.findByText('已屏蔽“测试科技”，匹配岗位已从列表隐藏，记录保留；解除屏蔽后恢复显示。')).toBeTruthy()
    expect(JSON.parse(String(requestsTo('/api/agent/config/apply')[0][1]?.body))).toEqual({
      preferences: { blocked_companies: ['已有屏蔽公司', '测试科技'] }, confirm: true,
    })
    await waitFor(() => expect(screen.queryByRole('button', { name: '屏蔽公司 测试科技' })).toBeNull())
    expect(await screen.findByText('没有符合当前条件的岗位')).toBeTruthy()
    expect(screen.queryByText('JD摘要')).toBeNull()
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST').map(([url]) => url)).toEqual([
      '/api/agent/config/preview', '/api/agent/config/apply',
    ])
  })

  it('hides jobs matching saved rules on every page mount and restores them when unblocked', async () => {
    rules = ['TEST']
    company = 'test 科技'
    const firstVisit = render(<DashboardPage view="jobs" />)
    expect(await screen.findByText('没有符合当前条件的岗位')).toBeTruthy()
    expect(screen.queryByText(company)).toBeNull()
    firstVisit.unmount()
    render(<DashboardPage view="jobs" />)
    expect(await screen.findByText('没有符合当前条件的岗位')).toBeTruthy()
    expect(screen.queryByText(company)).toBeNull()
    rules = []
    act(() => window.dispatchEvent(new Event('bosshunter-config-saved')))
    expect(await screen.findByRole('button', { name: '屏蔽公司 test 科技' })).toBeTruthy()
    expect(requestsTo('/api/agent/config/apply')).toHaveLength(0)
  })

  it('clears selected jobs when a saved blacklist hides them', async () => {
    render(<DashboardPage view="jobs" />)
    await screen.findByRole('button', { name: '屏蔽公司 测试科技' })
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 测试科技 前端工程师' }))
    expect(screen.getByText('已选择 1 条')).toBeTruthy()
    rules.push(company)
    act(() => window.dispatchEvent(new Event('bosshunter-config-saved')))
    expect(await screen.findByText('没有符合当前条件的岗位')).toBeTruthy()
    expect(screen.getByText('已选择 0 条')).toBeTruthy()
    expect(requestsTo('/api/agent/config/apply')).toHaveLength(0)
  })

  it('requires a new preview if another page changes the list during save', async () => {
    rulesAfterPreview = ['已有屏蔽公司', '另一个页面新增的公司']
    await clickBlock()
    expect((await screen.findAllByText('屏蔽名单已变化，请重新点击“屏蔽公司”查看最新预览。')).length).toBeGreaterThanOrEqual(1)
    expect(requestsTo('/api/agent/config/apply')).toHaveLength(0)
    expect(rules).toContain('另一个页面新增的公司')
  })

  it('shows the backend error and allows retry when saving is blocked', async () => {
    applyError = '采集任务运行中，请结束后再修改配置'
    const button = await clickBlock()
    // 提示必须落在被点击的那张卡片上，而不是只在页面顶部（长列表里用户看不到）。
    expect((await screen.findAllByText(applyError)).length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByRole('status').some(node => node.textContent === applyError)).toBe(true)
    expect(button.disabled).toBe(false)
    expect(button.textContent).toBe('屏蔽公司')
    expect(rules).toEqual(['已有屏蔽公司'])
  })

  it('does not save if preview validation fails', async () => {
    previewError = '公司名超过长度限制'
    await clickBlock()
    expect((await screen.findAllByText(previewError)).length).toBeGreaterThanOrEqual(1)
    expect(requestsTo('/api/agent/config/apply')).toHaveLength(0)
  })

  it('disables blocking when the company name is missing', async () => {
    company = ''
    const button = await clickBlock()
    expect(button.disabled).toBe(true)
    expect(requestsTo('/api/agent/config/preview')).toHaveLength(0)
  })
})

describe('工作台待确认岗位屏蔽公司', () => {
  let rules: string[]
  let company: string
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/workbench') {
      return jsonResponse({
        funnel: {}, funnel_today: {},
        pending_confirmation: [{ id: 'job-1', company, title: '前端工程师', status: 'ready', score: 90, created_at: '', greeting: '你好' }],
        pending_greetings: [], send_errors: [], needs_resume: [], task: null, last_task: null,
        send_quota: { daily_limit: 30, sent: 0, remaining: 30, exhausted: false },
      })
    }
    if (url === '/api/agent/state') {
      return jsonResponse({ preferences: { blocked_companies: [...rules] } })
    }
    if (url === '/api/agent/config/preview') {
      return jsonResponse({ preferences: JSON.parse(String(init?.body)).preferences, requires_confirmation: true })
    }
    if (url === '/api/agent/config/apply') {
      rules = JSON.parse(String(init?.body)).preferences.blocked_companies
      return jsonResponse({ success: true, preferences: { blocked_companies: rules } })
    }
    return jsonResponse([])
  })

  const requestsTo = (path: string) => fetchMock.mock.calls.filter(([url]) => String(url) === path)

  beforeEach(() => {
    rules = ['已有屏蔽公司']
    company = '测试科技'
    fetchMock.mockClear()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('blocks the company from a workbench card and hides the job', async () => {
    render(<DashboardPage view="workbench" />)
    fireEvent.click(await screen.findByRole('button', { name: '屏蔽公司 测试科技' }))
    expect((await screen.findAllByText('已屏蔽“测试科技”，匹配岗位已从列表隐藏，记录保留；解除屏蔽后恢复显示。')).length).toBeGreaterThan(0)
    expect(JSON.parse(String(requestsTo('/api/agent/config/apply')[0][1]?.body))).toEqual({
      preferences: { blocked_companies: ['已有屏蔽公司', '测试科技'] }, confirm: true,
    })
    expect(await screen.findByText('今天暂时没有待确认岗位。')).toBeTruthy()
  })

  it('hides workbench jobs matching saved rules on mount', async () => {
    rules = ['TEST']
    company = 'test 科技'
    render(<DashboardPage view="workbench" />)
    expect(await screen.findByText('今天暂时没有待确认岗位。')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '屏蔽公司 test 科技' })).toBeNull()
    expect(requestsTo('/api/agent/config/apply')).toHaveLength(0)
  })
})
