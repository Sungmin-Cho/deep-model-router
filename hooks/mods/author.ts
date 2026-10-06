// The author-declaration pre-check: warnings only. The router never infers
// who wrote the work under review (references/review-policy.md); this check
// compares a route's seats with the model this Claude Code session actually
// runs, which only the host knows, and says so when they match.

import type { DispatchRun } from './shell'

/** `model-opus[1m]` -> `model-opus`: the session reports the context variant too. */
export function normalizeModel(id: string): string {
  return id.trim().toLowerCase().replace(/\[[^\]]*\]$/, '')
}

export type Finding = {
  /** A key for "show once per session" findings; undefined repeats. */
  once?: string
  text: string
}

type Seat = { seat: string; model: string }

/** The seats a route asks the caller to dispatch, by model id. */
export function seatedModels(route: Record<string, unknown>): Seat[] {
  const out: Seat[] = []
  const listed = route['dispatch_seats']
  if (Array.isArray(listed)) {
    for (const s of listed as Record<string, unknown>[]) {
      if (typeof s?.['model_id'] === 'string') out.push({ seat: String(s['seat'] ?? 'seat'), model: s['model_id'] })
    }
    return out
  }
  const review = (route['review'] ?? {}) as Record<string, unknown>
  const models = Array.isArray(review['reviewer_models']) ? review['reviewer_models'] as unknown[] : []
  models.forEach((m, i) => { if (typeof m === 'string') out.push({ seat: `reviewer-${i + 1}`, model: m }) })
  if (typeof review['judge_model'] === 'string') out.push({ seat: 'judge', model: review['judge_model'] })
  return out
}

/** A RouteDecisionV1 document, or undefined for anything else on stdout. */
export function parseRoute(stdout: string): Record<string, unknown> | undefined {
  const text = stdout.trim()
  if (!text.startsWith('{')) return undefined
  try {
    const doc = JSON.parse(text) as unknown
    if (doc !== null && typeof doc === 'object' && !Array.isArray(doc)
      && 'route_schema_version' in doc) return doc as Record<string, unknown>
  } catch {
    // Not one JSON document: a text route, or output piped elsewhere.
  }
  return undefined
}

export function routeFindings(route: Record<string, unknown>, sessionModel: string): Finding[] {
  if (route['terminal'] !== null && route['terminal'] !== undefined) return []
  const findings: Finding[] = []
  const mine = normalizeModel(sessionModel)
  const hits = seatedModels(route).filter(s => normalizeModel(s.model) === mine)
  if (hits.length > 0) {
    findings.push({
      text: `${hits.map(h => h.seat).join(', ')} is this session's model ${mine}. If this session wrote the work under review, `
        + 'declare it (implementer.model_id, or review_context.author_model_ids for a REVIEW task) and route again — '
        + 'the router never infers authorship from the host.',
    })
  }
  const declared = (route['review_context'] !== undefined && route['review_context'] !== null)
    || route['implementer_declared'] === true
  if (route['task_class'] === 'REVIEW' && !declared) {
    findings.push({
      once: 'review-undeclared',
      text: 'REVIEW route without review_context: the router cannot keep the target\'s authors off its seats. '
        + 'Declare review_context.author_model_ids or author_families in --request-json.',
    })
  }
  return findings
}

/** Long enough that idle sleep is a real risk (the shortest per-seat default). */
export const CAFFEINATE_AFTER_SECONDS = 600

export function dispatchFindings(run: DispatchRun, platform: string | undefined): Finding[] {
  const findings: Finding[] = []
  if (run.cli === 'claude' && run.childArgv.includes('--bare')) {
    findings.push({
      text: `${run.attemptId}: claude --bare skips keychain auth, so the seat fails "Not logged in". `
        + 'Drop --bare (references/adapters.md, Claude Code transport).',
    })
  }
  if (platform === 'Darwin' && (run.deadlineSeconds ?? 0) >= CAFFEINATE_AFTER_SECONDS
    && !run.wrappers.includes('caffeinate')) {
    findings.push({
      once: 'caffeinate',
      text: 'macOS idle sleep can freeze a long dispatch and its supervisor, leaving the receipt RUNNING. '
        + 'Wrap long dispatches in `caffeinate -i` (it does not stop lid-close sleep).',
    })
  }
  return findings
}
