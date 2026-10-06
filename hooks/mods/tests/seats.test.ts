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
    expect(w.statuses.at(-1)).toBe('seats: codex·model-sol NO_RECEIPT')
  })

  test('a relative receipt dir follows a literal cd in the same command', async ($, on) => {
    const w = world(on, { cwd: '/repo', proc: statusSequence(receipt('a1', 'RUNNING')) })
    await startSession($)
    await $.tool.call({
      tool: 'Bash', run_in_background: true,
      command: 'cd sub && nohup python3 ../dispatch_agent.py run --attempt-id=a1 --receipt-dir=../out/r --seat worker -- grok -p x > log 2>&1 &',
    })
    await w.clock.settle()
    expect(w.runs.find(isStatus)).toEqual(expect.arrayContaining(['--receipt-dir', '/repo/out/r']))
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
    expect(w.fills.at(-1)).toMatch(/verify-evidence --receipt-dir \/home\/me\/receipts --ids r1-sol --expect-count 1 --expect-models model-sol$/)
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

  test('nothing is tracked where no surface draws (a -p child seat)', async ($, on) => {
    const w = world(on, { surfaces: [], proc: statusSequence(receipt('r1-sol', 'RUNNING')) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH, run_in_background: true })
    await w.clock.advance(30_000)
    expect(w.runs).toEqual([])
    expect(w.statuses.filter(s => s !== undefined)).toEqual([])
  })
})
