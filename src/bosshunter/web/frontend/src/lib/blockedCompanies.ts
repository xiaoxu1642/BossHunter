export function findBlockingRule(company: string | undefined, rules: string[]): string | undefined {
  const normalized = (company || '').trim().toLowerCase()
  if (!normalized) return undefined
  return rules.find(rule => rule.trim() && normalized.includes(rule.trim().toLowerCase()))
}
