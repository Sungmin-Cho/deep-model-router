import { describe, expect, test } from 'claude-code/testing'

import {
  FAILURE_LIMIT, PENDING_LIMIT_MS, announcements, applyStatus, attention, newSeat, statusLine,
} from '../seats'
import { T0, receipt } from './world'

const seat = (patch: Record<string, unknown> = {}) => ({
  ...newSeat({ attemptId: 'a1', seat: 'reviewer-1', runtime: null, modelId: 'model-x', cli: 'codex',
    deadlineSeconds: 600, fingerprint: null }, '/r', T0, 'command'),
  ...patch,
})
const ok = (attemptId: string, state: string, extra = {}) => ({ exitCode: 0, stderr: '', ...receipt(attemptId, state, extra) }) as
  { exitCode: number; stdout: string; stderr: string }

describe('reading `status`', () => {
  test('terminal states end polling; RUNNING and CLAIMED do not', () => {
    expect(applyStatus(seat(), ok('a1', 'FAILED'), T0)).toMatchObject({ state: 'FAILED', final: true, finalAt: T0 })
    expect(applyStatus(seat(), ok('a1', 'RUNNING', { supervision: 'supervised', processAlive: true }), T0))
      .toMatchObject({ state: 'RUNNING', final: false, supervision: 'supervised', processAlive: true })
    const claimed = { exitCode: 0, stdout: '{"attempt_id": "a1", "state": "CLAIMED"}', stderr: '' }
    expect(applyStatus(seat(), claimed, T0)).toMatchObject({ state: 'CLAIMED', final: false })
  })

  test('an unknown attempt waits, then ends as NO_RECEIPT; at once when its command already returned', () => {
    const unknown = { exitCode: 2, stdout: '', stderr: "attempt 'a1' is unknown under /r — no receipt and no claim" }
    expect(applyStatus(seat(), unknown, T0 + 1000)).toMatchObject({ state: 'PENDING', final: false })
    expect(applyStatus(seat(), unknown, T0 + PENDING_LIMIT_MS + 1)).toMatchObject({ state: 'NO_RECEIPT', final: true })
    expect(applyStatus(seat({ commandDone: true }), unknown, T0 + 1)).toMatchObject({ state: 'NO_RECEIPT', final: true })
  })

  test('a refused success and an unconfirmed termination with a held claim need a person', () => {
    const refused = { exitCode: 2, stdout: '', stderr: 'invalid completion receipt: stdout digest mismatch' }
    const r = applyStatus(seat(), refused, T0)
    expect(r).toMatchObject({ state: 'INVALID_RECEIPT', final: true })
    expect(attention(r, T0)).toBe('invalid receipt')
    const held = { exitCode: 5, stdout: '', stderr: 'receipt publication is incomplete (claim retained): TERMINATION_UNCONFIRMED' }
    const h = applyStatus(seat(), held, T0)
    expect(h).toMatchObject({ state: 'TERMINATION_UNCONFIRMED', final: true })
    expect(attention(h, T0)).toBe('TERMINATION_UNCONFIRMED')
  })

  test('readings that keep failing end as UNREADABLE, flagged; one good reading resets the count', () => {
    const bad = { exitCode: 8, stdout: '', stderr: 'receipt publication is incomplete (claim retained): SUCCEEDED' }
    let s = seat()
    for (let i = 1; i < FAILURE_LIMIT; i += 1) {
      s = applyStatus(s, bad, T0)
      expect(s).toMatchObject({ final: false, failures: i, detail: expect.stringContaining('claim retained') })
    }
    expect(applyStatus(s, ok('a1', 'RUNNING'), T0)).toMatchObject({ failures: 0, detail: null })
    s = applyStatus(s, { exitCode: -1, stdout: '', stderr: 'Error: spawn python3 ENOENT' }, T0)
    expect(s).toMatchObject({ state: 'UNREADABLE', final: true })
    expect(attention(s, T0)).toBe('unreadable')
    expect(applyStatus(seat(), { exitCode: 0, stdout: 'not json', stderr: '' }, T0)).toMatchObject({ failures: 1, final: false })
  })

  test('announcements name a finish once and each attention label once', () => {
    const running = applyStatus(seat(), ok('a1', 'RUNNING', { supervision: 'orphaned' }), T0)
    const first = announcements([seat()], [running], T0)
    expect(first.toasts).toEqual([expect.stringMatching(/orphaned/)])
    const again = announcements(first.seats, first.seats, T0)
    expect(again.toasts).toEqual([])
    const done = applyStatus(first.seats[0]!, ok('a1', 'CANCELLED', { finishedAt: '2026-10-06T00:03:00Z' }), T0)
    expect(announcements(first.seats, [done], T0).toasts).toEqual(['reviewer-1 codex·model-x CANCELLED in 3m'])
  })

  test('more than four visible seats collapse into counts', () => {
    const many = [1, 2, 3, 4, 5].map(i => seat({ attemptId: `a${i}`, state: i < 3 ? 'RUNNING' : 'SUCCEEDED',
      final: i >= 3, finalAt: i >= 3 ? T0 : null, supervision: i === 1 ? 'stale' : null }))
    expect(statusLine(many, T0)).toBe('seats: 2 running · 3 finished · ⚠ 1 need a person — /router-seats')
    expect(statusLine(many.map(s => ({ ...s, final: true, finalAt: T0, supervision: null })), T0 + 11 * 60_000)).toBeUndefined()
  })
})
