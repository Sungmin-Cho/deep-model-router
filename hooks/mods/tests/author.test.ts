import { describe, expect, test } from 'claude-code/testing'

import { DISPATCH, receipt, startSession, world } from './world'

const ROUTE = 'python3 "$SKILL_DIR"/scripts/route_task.py --request-json req.json --format json'

const route = (extra: Record<string, unknown>) => JSON.stringify({
  route_schema_version: 1, task_class: 'IMPLEMENTATION', terminal: null, implementer_declared: false,
  review: { reviewer_models: ['model-opus', 'model-sol'], judge_model: null }, ...extra,
}, null, 2)

describe('author-declaration check', () => {
  test('a reviewer seat holding this session\'s model is warned about, not blocked', async ($, on) => {
    const w = world(on, { bash: () => ({ stdout: route({}) }) })
    await startSession($)
    const ran = await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(ran.deny).toBeUndefined()
    expect(w.toasts).toHaveLength(1)
    expect(w.toasts[0]).toMatch(/^reviewer-1 is this session's model model-opus\. If this session wrote/)
    expect(w.toasts[0]).toMatch(/implementer\.model_id/)
  })

  test('dispatch_seats is the list read when the route has one', async ($, on) => {
    const w = world(on, {
      model: 'model-sol',
      bash: () => ({
        stdout: route({
          implementer_declared: true,
          dispatch_seats: [{ seat: 'reviewer-1', model_id: 'model-opus' }, { seat: 'reviewer-2', model_id: 'model-sol' }],
        }),
      }),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(w.toasts).toEqual([expect.stringMatching(/^reviewer-2 is this session's model model-sol/)])
  })

  test('one decision is warned about once; a declared author gets the correction wording (review i1)', async ($, on) => {
    let doc = route({ decision_fingerprint: 'f1' })
    const w = world(on, { bash: () => ({ stdout: doc }) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(w.toasts).toHaveLength(1)
    doc = route({ decision_fingerprint: 'f2', implementer_declared: true })
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(w.toasts).toHaveLength(2)
    expect(w.toasts[1]).toMatch(/the route declares another author\. If this session wrote the work under review, correct the declaration/)
  })

  test('no warning when the session model is not seated, or the route is terminal', async ($, on) => {
    let doc = route({ review: { reviewer_models: ['model-sol', 'grok-model'], judge_model: null } })
    const w = world(on, { bash: () => ({ stdout: doc }) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    doc = route({ terminal: 'HUMAN_REQUIRED' })
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(w.toasts).toEqual([])
  })

  test('a route printed with a nonzero exit (human gate) is still read', async ($, on) => {
    const w = world(on, { bash: () => ({ stdout: route({}), isError: true, exitCode: 3 }) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(w.toasts).toHaveLength(1)
  })

  test('a REVIEW route without review_context gets a hint once per session', async ($, on) => {
    const w = world(on, {
      model: 'model-fable',
      bash: () => ({ stdout: route({ task_class: 'REVIEW' }) }),
    })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    await $.tool.call({ tool: 'Bash', command: ROUTE })
    expect(w.toasts).toEqual([expect.stringMatching(/^REVIEW route without review_context/)])
  })

  test('output that is not a route (text format, piped to jq) is ignored', async ($, on) => {
    const w = world(on, { bash: () => ({ stdout: 'IMPLEMENTATION scored 9/18 ... model-opus' }) })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: 'python3 route_task.py --class IMPLEMENTATION | jq .review' })
    expect(w.toasts).toEqual([])
  })

  test('claude --bare in a dispatched seat is warned about', async ($, on) => {
    const w = world(on, { proc: () => receipt('c1', 'RUNNING') })
    await startSession($)
    await $.tool.call({
      tool: 'Bash', run_in_background: true,
      command: 'caffeinate -i python3 dispatch_agent.py run --attempt-id c1 --receipt-dir /r --deadline-seconds 600 '
        + '--seat reviewer-1 --model-id model-opus -- claude -p --bare --model model-opus --effort high',
    })
    expect(w.toasts).toEqual([expect.stringMatching(/^c1: claude --bare skips keychain auth/)])
  })

  test('a long dispatch on macOS without caffeinate gets one hint per session', async ($, on) => {
    const w = world(on, { platform: 'Darwin', proc: () => receipt('r1-sol', 'RUNNING') })
    await startSession($)
    const bare = DISPATCH.replace('caffeinate -i ', '')
    await $.tool.call({ tool: 'Bash', command: bare, run_in_background: true })
    await $.tool.call({ tool: 'Bash', command: bare.replace('r1-sol', 'r2-sol'), run_in_background: true })
    await $.tool.call({ tool: 'Bash', command: DISPATCH.replace('r1-sol', 'r3-sol'), run_in_background: true })
    expect(w.toasts.filter(t => t.includes('caffeinate'))).toHaveLength(1)
  })

  test('two long dispatches in one call give the caffeinate hint once (review i3)', async ($, on) => {
    const w = world(on, { platform: 'Darwin', proc: () => receipt('r1-sol', 'RUNNING') })
    await startSession($)
    const bare = DISPATCH.replace('caffeinate -i ', '')
    await $.tool.call({ tool: 'Bash', command: `${bare} &\n${bare.replace('r1-sol', 'r2-sol')} &`, run_in_background: true })
    expect(w.toasts).toHaveLength(1)
    expect(w.toasts[0]!.match(/caffeinate -i/g)).toHaveLength(1)
  })

  test('no caffeinate hint off macOS', async ($, on) => {
    const w = world(on, { platform: 'Linux', proc: () => receipt('r1-sol', 'RUNNING') })
    await startSession($)
    await $.tool.call({ tool: 'Bash', command: DISPATCH.replace('caffeinate -i ', ''), run_in_background: true })
    expect(w.toasts.filter(t => t.includes('caffeinate'))).toEqual([])
  })
})
