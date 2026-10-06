import { describe, expect, test } from 'claude-code/testing'

import { DISPATCH, PLUGIN, command, paneProps, receipt, startSession, world } from './world'
import type { Proc } from './world'

const isStatus = (argv: readonly string[]) => argv.includes('status') && argv.some(a => a.endsWith('dispatch_agent.py'))

/** Answers each `status` call from the list in turn, the last one repeating. */
const statusSequence = (...readings: Proc[]) => {
  let i = 0
  return (argv: readonly string[]): Proc => {
    if (!isStatus(argv)) return { exitCode: 2, stderr: 'unexpected' }
    const r = readings[Math.min(i, readings.length - 1)]!
    i += 1
    return r
  }
}

describe('seat tracker', () => {
  test('a dispatch in a Bash call is tracked and shown on the status line', async ($, on) => {
    const w = world(on, { proc: statusSequence(receipt('r1-sol', 'RUNNING', { supervision: 'supervised', processAlive: true })) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    const status = w.runs.find(isStatus)
    expect(status).toEqual(expect.arrayContaining(['--attempt-id', 'r1-sol', '--receipt-dir', '/home/me/receipts']))
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol RUNNING 0s/20m')
    expect(w.toasts).toEqual([])
  })

  test('a terminal state is toasted once, with the verdict and the time it took', async ($, on) => {
    const w = world(on, {
      proc: statusSequence(
        receipt('r1-sol', 'RUNNING', { supervision: 'supervised', processAlive: true }),
        receipt('r1-sol', 'SUCCEEDED', { verdict: 'PASS', finishedAt: '2026-10-06T00:07:00Z' }),
      ),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    expect(w.toasts).toEqual([])
    await w.clock.advance(20_000)
    expect(w.toasts).toEqual(['reviewer-1 codex·model-sol SUCCEEDED PASS in 7m'])
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol SUCCEEDED PASS')
    const polls = w.runs.filter(isStatus).length
    await w.clock.advance(60_000)
    expect(w.runs.filter(isStatus).length).toBe(polls)
    expect(w.toasts).toHaveLength(1)
  })

  test('an orphaned seat and an unconfirmed termination ask for a person', async ($, on) => {
    const w = world(on, {
      proc: statusSequence(
        receipt('r1-sol', 'RUNNING', { supervision: 'orphaned', processAlive: true }),
        receipt('r1-sol', 'TERMINATION_UNCONFIRMED'),
      ),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    expect(w.toasts).toHaveLength(1)
    expect(w.toasts[0]).toMatch(/^⚠ reviewer-1 codex·model-sol: orphaned — .*cancel/)
    expect(w.statuses.at(-1)).toMatch(/^seats: ⚠ codex·model-sol RUNNING .* — \/router-seats$/)
    await w.clock.advance(20_000)
    expect(w.toasts).toHaveLength(2)
    expect(w.toasts[1]).toMatch(/TERMINATION_UNCONFIRMED — .*termination_unconfirmed/)
    // Flagged seats stay on the line until a person clears them.
    await w.clock.advance(15 * 60_000)
    expect(w.statuses.at(-1)).toMatch(/^seats: ⚠ codex·model-sol TERMINATION_UNCONFIRMED/)
    const cleared = await $.command.run(command('router-seats', 'clear'))
    expect(cleared.text).toBe('Cleared 1 finished seat(s).')
    expect(w.statuses.at(-1)).toBeUndefined()
  })

  test('a RUNNING receipt past its deadline reads as overdue', async ($, on) => {
    const w = world(on, {
      proc: statusSequence(receipt('r1-sol', 'RUNNING', {
        supervision: 'supervised', processAlive: true, deadlineAt: '2026-10-05T23:50:00Z',
      })),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    expect(w.toasts[0]).toMatch(/overdue/)
  })

  test('a command it cannot read for certain is left alone', async ($, on) => {
    const w = world(on, { proc: statusSequence(receipt('x', 'RUNNING')) })
    await startSession($)
    for (const command of [
      'python3 dispatch_agent.py run --attempt-id "$ID" --receipt-dir r -- codex exec -',
      'python3 dispatch_agent.py run --attempt-id a1 --receipt-dir "$(mktemp -d)" -- codex exec -',
      'cd "$WORK" && python3 dispatch_agent.py run --attempt-id a1 --receipt-dir r -- codex exec -',
      "python3 - <<'PY'\nimport os; os.system('python3 dispatch_agent.py run --attempt-id a1 --receipt-dir r -- x')\nPY",
      'python3 dispatch_agent.py status --attempt-id a1 --receipt-dir r',
      'grep -n dispatch_agent.py README.md',
    ]) {
      await $.tool.call({ tool: 'Bash', command })
    }
    await w.clock.advance(60_000)
    expect(w.runs.filter(isStatus)).toEqual([])
    expect(w.toasts).toEqual([])
    expect(w.statuses.filter(s => s !== undefined)).toEqual([])
  })

  test('a foreground dispatch that returns without a receipt never started', async ($, on) => {
    const w = world(on, {
      proc: () => ({ exitCode: 2, stderr: "attempt 'r1-sol' is unknown under /home/me/receipts — no receipt and no claim" }),
      bash: () => ({ isError: true, exitCode: 2 }),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH })
    await w.clock.settle()
    // The first reading may precede the supervisor's claim: one more poll confirms.
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol PENDING')
    await w.clock.advance(20_000)
    expect(w.statuses.at(-1)).toBe('seats: ⚠ codex·model-sol NO_RECEIPT — /router-seats')
    expect(w.toasts).toEqual([expect.stringMatching(/^⚠ reviewer-1 codex·model-sol: no receipt — /)])
    const polls = w.runs.filter(isStatus).length
    await w.clock.advance(5 * 60_000)
    expect(w.runs.filter(isStatus)).toHaveLength(polls)
  })

  test('a relative receipt dir resolves against the shell directory, unless the command moved it', async ($, on) => {
    const w = world(on, { cwd: '/repo', proc: statusSequence(receipt('a1', 'RUNNING')) })
    await startSession($)
    await $.tool.call({
      tool: 'Bash', run_in_background: true,
      command: 'nohup python3 ../dispatch_agent.py run --attempt-id=a1 --receipt-dir=../out/r --seat worker -- grok -p x > log 2>&1 &',
    })
    await w.clock.settle()
    // `..` is left for the operating system: folding it across a symbolic link names another directory.
    expect(w.runs.find(isStatus)).toEqual(expect.arrayContaining(['--receipt-dir', '/repo/../out/r']))
    await $.tool.call({
      tool: 'Bash', run_in_background: true,
      command: 'cd sub && python3 dispatch_agent.py run --attempt-id a2 --receipt-dir r -- grok -p x',
    })
    await w.clock.settle()
    expect(w.runs.filter(isStatus).some(argv => argv.includes('a2'))).toBe(false)
  })

  test('the seats pane draws each attempt and its buttons only fill the prompt', async ($, on) => {
    const running = receipt('r1-sol', 'RUNNING', { supervision: 'supervised', processAlive: true })
    const w = world(on, {
      proc: statusSequence(
        running, running,
        receipt('r1-sol', 'SUCCEEDED', { verdict: 'PASS_WITH_CHANGES', finishedAt: '2026-10-06T00:09:00Z' }),
      ),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    const opened = await $.command.run(command('router-seats', ''))
    await w.clock.settle()
    expect(opened.text).toBe('Router seats: 1 tracked this session.')
    expect(w.runs.filter(isStatus)).toHaveLength(2)
    expect(w.opened).toEqual(['router-seats'])
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({
        plugin: PLUGIN, surface, component: 'Pane', requestId: 'router-seats',
        props: paneProps('Router seats'),
        viewport: { columns: 120, rows: 40 },
      })
      expect((await ui.find({ type: 'Text', text: /codex·model-sol/ }))).toBeDefined()
      await ui.press({ key: 'cancel-0' })
      expect(w.fills.at(-1)).toMatch(/^! python3 \S+\/skills\/model-router\/scripts\/dispatch_agent\.py cancel --attempt-id r1-sol --receipt-dir \/home\/me\/receipts$/)
      await ui.unmount()
    }
    await w.clock.advance(20_000)
    const ui = await $.ui.mount({
      plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: 'router-seats',
      props: paneProps('Router seats'),
    })
    expect(await ui.find({ key: 'cancel-0' })).toBeUndefined()
    await ui.press({ key: 'verify-0' })
    // The expectations come from the route, never from the receipts under check.
    expect(w.fills.at(-1)).toMatch(/verify-evidence --receipt-dir \/home\/me\/receipts --ids r1-sol --expect-count $/)
    await ui.press({ key: 'status-0' })
    expect(w.fills.at(-1)).toMatch(/ status --attempt-id r1-sol --receipt-dir \/home\/me\/receipts$/)
    expect(w.bash).toEqual([DISPATCH])
    await ui.unmount()
  })

  test('/router-seats add tracks an attempt by hand; clear drops finished ones', async ($, on) => {
    const w = world(on, { cwd: '/repo', proc: statusSequence(receipt('m1', 'FAILED')) })
    await startSession($)
    const added = await $.command.run(command('router-seats', 'add receipts m1'))
    expect(added.text).toBe('Tracking m1 in /repo/receipts.')
    await w.clock.settle()
    expect(w.runs.find(isStatus)).toEqual(expect.arrayContaining(['--attempt-id', 'm1', '--receipt-dir', '/repo/receipts']))
    const bad = await $.command.run(command('router-seats', 'add receipts ../x'))
    expect(bad.text).toMatch(/^Usage/)
    const cleared = await $.command.run(command('router-seats', 'clear'))
    expect(cleared.text).toBe('Cleared 1 finished seat(s).')
    expect(w.statuses.at(-1)).toBeUndefined()
  })

  test('two seats on one model are told apart by seat; short runs read in seconds', async ($, on) => {
    const w = world(on, {
      proc: argv => receipt(argv[argv.indexOf('--attempt-id') + 1]!, 'SUCCEEDED', {
        modelId: 'model-haiku', finishedAt: '2026-10-06T00:00:42Z',
      }),
    })
    await startSession($)
    for (const id of ['h1', 'h2']) {
      await $.tool.call({
        tool: 'Bash', run_in_background: true,
        command: `python3 dispatch_agent.py run --attempt-id ${id} --receipt-dir /r --seat reviewer-${id.slice(1)} -- claude -p --model model-haiku`,
      })
      await w.clock.settle()
    }
    expect(w.statuses.at(-1)).toBe('seats: reviewer-1 claude·model-haiku SUCCEEDED · reviewer-2 claude·model-haiku SUCCEEDED')
    expect(w.toasts).toContain('reviewer-1 claude·model-haiku SUCCEEDED in 42s')
  })

  test('an attempt is not given up on while its Bash call still runs (review i1)', async ($, on) => {
    const unknown = { exitCode: 2, stderr: "attempt 'r1-sol' is unknown under /home/me/receipts — no receipt and no claim" }
    let started = false
    const w: ReturnType<typeof world> = world(on, {
      proc: () => (started ? receipt('r1-sol', 'RUNNING') : unknown),
      bash: async () => {
        // A permission prompt, or `sleep 300 && … run …`, before the dispatch starts.
        await w.clock.sleep(300_000)
        started = true
        return { background: true }
      },
    })
    await startSession($)
    const call = $.tool.call({ tool: 'Bash', command: DISPATCH })
    await w.clock.advance(280_000)
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol PENDING')
    expect(w.toasts).toEqual([])
    await w.clock.advance(40_000)
    await call
    await w.clock.advance(20_000)
    expect(w.statuses.at(-1)).toMatch(/^seats: codex·model-sol RUNNING /)
    expect(w.toasts).toEqual([])
  })

  test('a call another hook denies drops only what it added (review i1)', async ($, on) => {
    let deny = false
    const w = world(on, {
      proc: statusSequence(receipt('r1-sol', 'RUNNING', { supervision: 'supervised' })),
      bash: () => (deny ? { deny: 'not now' } : {}),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    deny = true
    // The same attempt again, denied: the record already tracked stays.
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toMatch(/^seats: codex·model-sol RUNNING/)
    // A new attempt, denied: gone again.
    await $.tool.call({ tool: 'Bash', command: DISPATCH.replace('r1-sol', 'r9-sol'), run_in_background: true })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toMatch(/^seats: codex·model-sol RUNNING [^·]*$/)
  })

  test('running the same attempt again keeps what was announced (review i1)', async ($, on) => {
    const w = world(on, {
      proc: statusSequence(receipt('r1-sol', 'SUCCEEDED', { verdict: 'PASS', finishedAt: '2026-10-06T00:01:00Z' })),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.advance(60_000)
    expect(w.toasts).toEqual(['reviewer-1 codex·model-sol SUCCEEDED PASS in 1m'])
  })

  test('a foreground call the tool moved to the background is not taken for a refusal', async ($, on) => {
    const unknown = { exitCode: 2, stderr: "attempt 'r1-sol' is unknown under /home/me/receipts — no receipt and no claim" }
    const w = world(on, { proc: () => unknown, bash: () => ({ background: true }) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol PENDING')
  })

  test('polling stops when only flagged or finished seats remain; the line clears on time (review i1)', async ($, on) => {
    const w = world(on, {
      proc: argv => (argv.includes('r1-sol') ? receipt('r1-sol', 'TERMINATION_UNCONFIRMED')
        : receipt('r2-sol', 'SUCCEEDED', { verdict: 'PASS' })),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await $.tool.call({ tool: 'Bash', command: DISPATCH.replace('r1-sol', 'r2-sol'), run_in_background: true })
    await w.clock.settle()
    const polls = w.runs.filter(isStatus).length
    await w.clock.advance(9 * 60_000)
    expect(w.runs.filter(isStatus)).toHaveLength(polls)
    expect(w.statuses.at(-1)).toMatch(/TERMINATION_UNCONFIRMED · reviewer-1 codex·model-sol SUCCEEDED PASS/)
    await w.clock.advance(2 * 60_000)
    expect(w.statuses.at(-1)).toBe('seats: ⚠ codex·model-sol TERMINATION_UNCONFIRMED — /router-seats')
    expect(w.runs.filter(isStatus)).toHaveLength(polls)
  })

  test('past the cap, finished records go and unfinished ones stay (review i1)', async ($, on) => {
    const w = world(on, {
      proc: argv => {
        const id = argv[argv.indexOf('--attempt-id') + 1]!
        return id === 'c0' ? receipt(id, 'RUNNING') : receipt(id, 'FAILED')
      },
    })
    await startSession($)
    for (let i = 0; i < 55; i += 1) {
      await $.tool.call({ tool: 'Bash', command: DISPATCH.replace('r1-sol', `c${i}`), run_in_background: true })
      await w.clock.settle()
    }
    await w.clock.advance(20_000)
    expect(w.runs.filter(isStatus).at(-1)).toEqual(expect.arrayContaining(['--attempt-id', 'c0']))
  })

  test('the whole flow runs the same in a desktop session', async ($, on) => {
    const w = world(on, {
      surfaces: ['desktop'],
      proc: statusSequence(
        receipt('r1-sol', 'RUNNING', { supervision: 'supervised' }),
        receipt('r1-sol', 'FAILED', { finishedAt: '2026-10-06T00:02:00Z' }),
      ),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.advance(20_000)
    expect(w.toasts).toEqual(['reviewer-1 codex·model-sol FAILED in 2m'])
    await $.command.run(command('router-seats'))
    const ui = await $.ui.mount({
      plugin: PLUGIN, surface: 'desktop', component: 'Pane', requestId: 'router-seats', props: paneProps('Router seats'),
    })
    expect(await ui.find({ type: 'Text', text: /FAILED/ })).toBeDefined()
    await ui.unmount()
  })

  test('a session start with seats still running picks the poll up again', async ($, on) => {
    const w = world(on, { proc: statusSequence(receipt('r1-sol', 'RUNNING', { supervision: 'supervised' })) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.settle()
    const polls = w.runs.filter(isStatus).length
    await startSession($)
    await w.clock.advance(20_000)
    expect(w.runs.filter(isStatus).length).toBeGreaterThan(polls)
  })

  test('a poll in flight when the Bash call returns keeps the return (review i2)', async ($, on) => {
    const unknown = { exitCode: 2, stderr: "attempt 'r1-sol' is unknown under /home/me/receipts — no receipt and no claim" }
    const w: ReturnType<typeof world> = world(on, {
      // Each status reading takes 15 s; the Bash call returns 30 s in, mid-poll.
      proc: async () => { await w.clock.sleep(15_000); return unknown },
      bash: async () => { await w.clock.sleep(30_000); return { background: true } },
    })
    await startSession($)
    const call = $.tool.call({ tool: 'Bash', command: DISPATCH })
    await w.clock.advance(31_000)
    await call
    await w.clock.advance(5 * 60_000)
    expect(w.statuses.at(-1)).toBe('seats: ⚠ codex·model-sol NO_RECEIPT — /router-seats')
  })

  test('a dispatch backgrounded by a group, subshell or pipeline is not read as waited for (review i2)', async ($, on) => {
    const w = world(on, {
      proc: argv => ({ exitCode: 2, stderr: `attempt '${argv[argv.indexOf('--attempt-id') + 1]}' is unknown under /r — no receipt and no claim` }),
    })
    await startSession($)
    for (const command of [
      '( python3 dispatch_agent.py run --attempt-id g1 --receipt-dir /r --seat s1 -- x ) &',
      'python3 dispatch_agent.py run --attempt-id g2 --receipt-dir /r --seat s2 -- x 2>&1 | tee log &',
    ]) await $.tool.call({ tool: 'Bash', command })
    await w.clock.advance(60_000)
    expect(w.statuses.at(-1)).toBe('seats: s1 x PENDING · s2 x PENDING')
  })

  test('one id twice in one call is one seat; the same id in two directories is two', async ($, on) => {
    const w = world(on, { proc: argv => receipt(argv[argv.indexOf('--attempt-id') + 1]!, 'RUNNING', { supervision: 'supervised' }) })
    await startSession($)
    await $.tool.call({
      tool: 'Bash', run_in_background: true,
      command: 'python3 dispatch_agent.py run --attempt-id a --receipt-dir /r --seat s1 -- x || '
        + 'python3 dispatch_agent.py run --attempt-id a --receipt-dir /r --seat s1 -- x; '
        + 'python3 dispatch_agent.py run --attempt-id a --receipt-dir /other --seat s2 -- x',
    })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toMatch(/^seats: s1 x·model-sol RUNNING \S+ · s2 x·model-sol RUNNING \S+$/)
  })

  test('a retried attempt that is denied puts the flagged record back (review i2)', async ($, on) => {
    let deny = false
    const w = world(on, {
      proc: () => ({ exitCode: 2, stderr: "attempt 'r1-sol' is unknown under /home/me/receipts — no receipt and no claim" }),
      bash: () => (deny ? { deny: 'not now' } : { isError: true, exitCode: 2 }),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH })
    await w.clock.advance(20_000)
    expect(w.statuses.at(-1)).toBe('seats: ⚠ codex·model-sol NO_RECEIPT — /router-seats')
    deny = true
    await $.tool.call({ tool: 'Bash', command: DISPATCH })
    await w.clock.settle()
    expect(w.statuses.at(-1)).toBe('seats: ⚠ codex·model-sol NO_RECEIPT — /router-seats')
    // Clearing drops a seat that never wrote a receipt.
    expect((await $.command.run(command('router-seats', 'clear'))).text).toBe('Cleared 1 finished seat(s).')
    expect(w.statuses.at(-1)).toBeUndefined()
  })

  test('a backgrounded dispatch with no receipt is flagged after the grace and dropped at the limit', async ($, on) => {
    const w = world(on, {
      proc: () => ({ exitCode: 2, stderr: "attempt 'r1-sol' is unknown under /home/me/receipts — no receipt and no claim" }),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.advance(100_000)
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol PENDING')
    await w.clock.advance(40_000)
    expect(w.statuses.at(-1)).toBe('seats: ⚠ codex·model-sol NO_RECEIPT — /router-seats')
    expect(w.toasts).toHaveLength(1)
    await w.clock.advance(30 * 60_000)
    const polls = w.runs.filter(isStatus).length
    await w.clock.advance(5 * 60_000)
    expect(w.runs.filter(isStatus)).toHaveLength(polls)
    expect(w.toasts).toHaveLength(1)
  })

  test('seats that finish together get one toast (the engine draws only the newest)', async ($, on) => {
    let done = false
    const w = world(on, {
      proc: argv => {
        const id = argv[argv.indexOf('--attempt-id') + 1]!
        return done ? receipt(id, 'SUCCEEDED', { verdict: 'PASS' }) : receipt(id, 'RUNNING')
      },
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await $.tool.call({ tool: 'Bash', command: DISPATCH.replace('r1-sol', 'r2-sol'), run_in_background: true })
    await w.clock.settle()
    done = true
    await w.clock.advance(20_000)
    expect(w.toasts).toEqual(['reviewer-1 codex·model-sol SUCCEEDED PASS · reviewer-1 codex·model-sol SUCCEEDED PASS'])
  })

  test('nothing is tracked where no surface draws (a -p child seat)', async ($, on) => {
    const w = world(on, { surfaces: [], proc: statusSequence(receipt('r1-sol', 'RUNNING')) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.advance(30_000)
    expect(w.runs).toEqual([])
    expect(w.statuses.filter(s => s !== undefined)).toEqual([])
  })
})
