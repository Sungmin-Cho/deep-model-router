// The seat tracker's pure half: what a `dispatch_agent.py status` reading
// means for one attempt, and how the attempts read on one status line.
// Nothing here runs a process or touches state.

import type { RouterSeat } from '../../types'
import type { DispatchRun } from './shell'

/** Receipt states after which the supervisor writes nothing more. */
export const TERMINAL = new Set([
  'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'START_FAILED',
  'TERMINATION_UNCONFIRMED', 'INVALID_OUTPUT',
])

/** How long an attempt may sit without a receipt before tracking gives up on it. */
export const PENDING_LIMIT_MS = 120_000
/** `status` readings that may fail in a row before an attempt is dropped as unreadable. */
export const FAILURE_LIMIT = 5
/** How long past `deadline_at` a RUNNING receipt reads as overdue (grace + KILL + confirmation). */
export const OVERDUE_AFTER_MS = 120_000
/** How long a finished attempt stays on the status line. */
export const SHOW_FINISHED_MS = 10 * 60_000

export function newSeat(run: Pick<DispatchRun, 'attemptId' | 'seat' | 'runtime' | 'modelId' | 'cli' | 'deadlineSeconds' | 'fingerprint'>,
  receiptDir: string, now: number, source: RouterSeat['source']): RouterSeat {
  return {
    attemptId: run.attemptId, receiptDir, seat: run.seat, runtime: run.runtime,
    modelId: run.modelId, cli: run.cli, deadlineSeconds: run.deadlineSeconds,
    fingerprint: run.fingerprint, source, trackedAt: now, commandDone: false,
    state: 'PENDING', startedAt: null, deadlineAt: null, finishedAt: null,
    supervision: null, processAlive: null, verdict: null, detail: null,
    final: false, finalAt: null, failures: 0, alerted: [],
  }
}

export const sameAttempt = (a: Pick<RouterSeat, 'attemptId' | 'receiptDir'>,
  b: Pick<RouterSeat, 'attemptId' | 'receiptDir'>) =>
  a.attemptId === b.attemptId && a.receiptDir === b.receiptDir

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
/** The last line of stderr: a traceback's verdict, or the supervisor's one-line refusal. */
const lastLine = (text: string) => text.trim().split('\n').pop()?.slice(0, 300) ?? ''

const finish = (seat: RouterSeat, now: number, patch: Partial<RouterSeat>): RouterSeat =>
  ({ ...seat, ...patch, final: true, finalAt: now })

export type StatusReading = { exitCode: number; stdout: string; stderr: string }

/**
 * Folds one `status` reading into the seat. `status` is the documented poll
 * (references/adapters.md, "Invoking the supervisor"): it prints the receipt,
 * adds liveness to a running one, and refuses a success whose evidence does
 * not hold together — so a disk SUCCEEDED is shown only once `status` agrees.
 */
export function applyStatus(seat: RouterSeat, reading: StatusReading, now: number): RouterSeat {
  if (reading.exitCode === 0) {
    let doc: Record<string, unknown>
    try {
      doc = JSON.parse(reading.stdout) as Record<string, unknown>
    } catch {
      return failed(seat, now, 'status printed no JSON')
    }
    if (doc['state'] === 'CLAIMED') {
      return { ...seat, state: 'CLAIMED', detail: null, failures: 0 }
    }
    const result = (doc['result'] ?? {}) as Record<string, unknown>
    const timing = (doc['timing'] ?? {}) as Record<string, unknown>
    const state = str(result['state'])
    if (state === null) return failed(seat, now, 'receipt has no result.state')
    const next: RouterSeat = {
      ...seat,
      state,
      seat: seat.seat ?? str(doc['seat']),
      runtime: seat.runtime ?? str(doc['runtime']),
      modelId: seat.modelId ?? str(doc['model_id']),
      fingerprint: seat.fingerprint ?? str(doc['decision_fingerprint']),
      startedAt: str(timing['started_at']),
      deadlineAt: str(timing['deadline_at']),
      finishedAt: str(timing['finished_at']),
      verdict: str(result['verdict']),
      supervision: str(doc['supervision']),
      processAlive: typeof doc['process_alive'] === 'boolean' ? doc['process_alive'] : null,
      detail: null,
      failures: 0,
    }
    return TERMINAL.has(state) ? finish(next, now, {}) : next
  }
  const said = lastLine(reading.stderr)
  if (reading.exitCode === 2 && said.includes('is unknown')) {
    // No receipt and no claim. A foreground dispatch that already returned
    // never started (a refusal before spawn); otherwise wait a while.
    const waited = now - seat.trackedAt
    if (seat.commandDone || waited > PENDING_LIMIT_MS) {
      return finish(seat, now, {
        state: 'NO_RECEIPT',
        detail: seat.commandDone ? 'the dispatch returned without a receipt (refused before spawn?)'
          : 'no receipt appeared',
      })
    }
    return { ...seat, state: 'PENDING', detail: null }
  }
  if (reading.exitCode === 2 && said.includes('invalid completion receipt')) {
    return finish(seat, now, { state: 'INVALID_RECEIPT', detail: said })
  }
  if (reading.exitCode === 5) {
    // A terminal write whose claim is still held, on an unconfirmed termination:
    // the same hold as TERMINATION_UNCONFIRMED itself.
    return finish(seat, now, { state: 'TERMINATION_UNCONFIRMED', detail: said })
  }
  return failed(seat, now, said || `status exited ${reading.exitCode}`)
}

function failed(seat: RouterSeat, now: number, detail: string): RouterSeat {
  const failures = seat.failures + 1
  return failures >= FAILURE_LIMIT
    ? finish(seat, now, { state: 'UNREADABLE', detail, failures })
    : { ...seat, detail, failures }
}

const parseTime = (iso: string | null) => {
  if (iso === null) return null
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

/** Why a person should look at this attempt now, or undefined. */
export function attention(seat: RouterSeat, now: number): string | undefined {
  if (seat.state === 'TERMINATION_UNCONFIRMED') return 'TERMINATION_UNCONFIRMED'
  if (seat.state === 'INVALID_RECEIPT') return 'invalid receipt'
  if (seat.state === 'UNREADABLE') return 'unreadable'
  if (seat.state === 'RUNNING' || seat.state === 'STARTING') {
    if (seat.supervision === 'orphaned' || seat.supervision === 'stale') return seat.supervision
    const deadline = parseTime(seat.deadlineAt)
    if (deadline !== null && now > deadline + OVERDUE_AFTER_MS) return 'overdue'
  }
  return undefined
}

/** What a person should do about an attention label. */
export function remedy(label: string): string {
  switch (label) {
    case 'TERMINATION_UNCONFIRMED':
      return 'a process may still write: confirm it is dead, then re-route with --flags termination_unconfirmed'
    case 'orphaned':
      return 'the child runs with no supervisor: cancel it before any retry'
    case 'stale':
      return 'nothing drives this receipt to a terminal state: cancel it before any retry'
    case 'overdue':
      return 'still RUNNING past its deadline: check for sleep or a network drop, then cancel'
    case 'invalid receipt':
      return 'status refused the claimed success: do not use it as review evidence'
    case 'unreadable':
      return 'status kept failing, so the mod stopped polling: press Status to see why'
    default:
      return ''
  }
}

/** `42s` under a minute, `7m` from there. */
const duration = (ms: number) => {
  const seconds = Math.max(0, Math.round(ms / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.round(seconds / 60)}m`
}

/** `codex·model-sol`, or whatever part of it is known. */
export function seatLabel(seat: RouterSeat): string {
  const who = [seat.cli, seat.modelId].filter((v): v is string => v !== null)
  if (who.length === 0) return seat.seat ?? seat.attemptId
  if (who.length === 2 && seat.modelId!.startsWith(`${seat.cli}`)) return seat.modelId!
  return who.join('·')
}

/** `RUNNING 4m/20m`, `SUCCEEDED PASS`, `PENDING`. */
export function stateText(seat: RouterSeat, now: number): string {
  if (seat.state === 'RUNNING' || seat.state === 'STARTING') {
    const started = parseTime(seat.startedAt) ?? seat.trackedAt
    const deadline = parseTime(seat.deadlineAt)
    const total = deadline !== null ? deadline - started
      : seat.deadlineSeconds !== null ? seat.deadlineSeconds * 1000 : null
    return `${seat.state} ${duration(now - started)}${total !== null ? `/${duration(total)}` : ''}`
  }
  return seat.verdict !== null ? `${seat.state} ${seat.verdict}` : seat.state
}

/** The attempts worth a glance: unfinished, needing attention, or finished a moment ago. */
export function visibleSeats(seats: readonly RouterSeat[], now: number): RouterSeat[] {
  return seats.filter(s => !s.final || attention(s, now) !== undefined
    || (s.finalAt !== null && now - s.finalAt < SHOW_FINISHED_MS))
}

/** The plugin's status line, or undefined to clear it. */
export function statusLine(seats: readonly RouterSeat[], now: number): string | undefined {
  const shown = visibleSeats(seats, now)
  if (shown.length === 0) return undefined
  const flagged = shown.filter(s => attention(s, now) !== undefined)
  if (shown.length <= 4) {
    const labels = shown.map(seatLabel)
    // Two seats on one model read alike: name the seat then.
    const named = shown.map((s, i) => (labels.filter(l => l === labels[i]).length > 1 && s.seat !== null
      ? `${s.seat} ${labels[i]}` : labels[i]))
    const parts = shown.map((s, i) => `${attention(s, now) !== undefined ? '⚠ ' : ''}${named[i]} ${stateText(s, now)}`)
    return `seats: ${parts.join(' · ')}${flagged.length > 0 ? ' — /router-seats' : ''}`
  }
  const running = shown.filter(s => !s.final).length
  const done = shown.length - running
  const parts = [`${running} running`, `${done} finished`]
  if (flagged.length > 0) parts.push(`⚠ ${flagged.length} need a person`)
  return `seats: ${parts.join(' · ')} — /router-seats`
}

/**
 * The toasts one refresh owes: a seat that reached a terminal label, and each
 * attention label the first time it shows. Returns the seats with what was
 * announced recorded, so a reload or the next tick does not repeat it.
 */
export function announcements(before: readonly RouterSeat[], after: readonly RouterSeat[], now: number):
  { seats: RouterSeat[]; toasts: string[] } {
  const toasts: string[] = []
  const seats = after.map(seat => {
    const prior = before.find(b => sameAttempt(b, seat))
    const alerted = [...seat.alerted]
    const label = attention(seat, now)
    if (label !== undefined && !alerted.includes(label)) {
      alerted.push(label)
      toasts.push(`⚠ ${seat.seat ?? 'seat'} ${seatLabel(seat)}: ${label} — ${remedy(label)} (/router-seats)`)
    } else if (seat.final && prior !== undefined && !prior.final && label === undefined) {
      const started = parseTime(seat.startedAt)
      const ended = parseTime(seat.finishedAt)
      const took = started !== null && ended !== null ? ` in ${duration(ended - started)}` : ''
      toasts.push(`${seat.seat ?? 'seat'} ${seatLabel(seat)} ${stateText(seat, now)}${took}`)
    }
    return alerted.length === seat.alerted.length ? seat : { ...seat, alerted }
  })
  return { seats, toasts }
}
