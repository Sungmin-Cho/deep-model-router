// The model-sync notice: one reading of `model_sync.py status`, folded into
// what `/router-sync` shows and the one toast a session start may owe.

import type { RouterSyncReport } from '../../types'

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const obj = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}

export function syncReport(stdout: string, exitCode: number, stderr: string, now: number): RouterSyncReport {
  const empty: RouterSyncReport = {
    at: now, autoUpgrade: 'unknown', retirements: [], deferred: [], inFlight: [], candidates: [], error: null,
  }
  if (exitCode !== 0) {
    return { ...empty, error: stderr.trim().split('\n').pop()?.slice(0, 300) || `status exited ${exitCode}` }
  }
  let doc: Record<string, unknown>
  try {
    doc = obj(JSON.parse(stdout))
  } catch {
    return { ...empty, error: 'status printed no JSON' }
  }
  const list = (v: unknown) => (Array.isArray(v) ? v.map(obj) : [])
  return {
    at: now,
    autoUpgrade: str(doc['auto_upgrade']) ?? 'unknown',
    retirements: list(doc['retirement_notices']).map(r => ({
      key: str(r['key']) ?? '?', id: str(r['id']) ?? '?',
      retirementAt: str(r['retirement_at']), upgradeTo: str(r['upgrade_to']),
    })),
    deferred: Object.entries(obj(doc['deferred'])).map(([key, v]) => {
      const n = obj(v)
      return { key, id: str(n['id']) ?? '?', reason: str(n['reason']), expired: n['expired'] === true }
    }),
    inFlight: list(doc['in_flight']).map(r => ({
      attemptId: str(r['attempt_id']) ?? '?', receiptDir: str(r['receipt_dir']) ?? '?',
    })),
    candidates: Object.entries(obj(doc['candidates'])).map(([key, id]) => ({ key, id: String(id) })),
    error: null,
  }
}

/** The session-start toast, or undefined when nothing needs a look (or auto-upgrade is off). */
export function syncNotice(report: RouterSyncReport): string | undefined {
  if (report.error !== null || report.autoUpgrade === 'disabled') return undefined
  const parts: string[] = []
  const n = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`
  if (report.retirements.length > 0) parts.push(n(report.retirements.length, 'retirement notice', 'retirement notices'))
  const expired = report.deferred.filter(d => d.expired).length
  if (expired > 0) parts.push(n(expired, 'deferred probe due again', 'deferred probes due again'))
  if (report.inFlight.length > 0) parts.push(n(report.inFlight.length, 'probe run in flight', 'probe runs in flight'))
  return parts.length > 0 ? `model-sync: ${parts.join(' · ')} — /router-sync` : undefined
}
