import { describe, expect, test } from 'claude-code/testing'

import { PLUGIN, command, paneProps, startSession, world } from './world'

const status = (extra: Record<string, unknown>) => JSON.stringify({
  auto_upgrade: 'enabled', retirement_notices: [], candidates: {}, deferred: {}, in_flight: [], ...extra,
})

const isSync = (argv: readonly string[]) => argv.some(a => a.endsWith('model_sync.py')) && argv.includes('status')

describe('model-sync notice', () => {
  test('a retirement notice, an expired deferral and a run in flight are toasted once', async ($, on) => {
    const w = world(on, {
      proc: argv => (isSync(argv) ? {
        stdout: status({
          retirement_notices: [{ key: 'openai_reasoning', id: 'model-sol', retirement_at: '2026-12-01', upgrade_to: 'model-sol-next' }],
          deferred: { claude_senior: { id: 'model-opus-next', reason: 'quota', expired: true }, xai: { id: 'grok-next', expired: false } },
          in_flight: [{ attempt_id: 'p1', receipt_dir: '/state/r' }],
        }),
      } : { exitCode: 2 }),
    })
    await startSession($)
    expect(w.runs.filter(isSync)).toEqual([])
    await w.clock.advance(5_000)
    expect(w.runs.filter(isSync)).toHaveLength(1)
    expect(w.toasts).toEqual([
      'model-sync: 1 retirement notice · 1 deferred probe due again · 1 probe run in flight — /router-sync',
    ])
    // A hot reload fires session.start again; the notice is not repeated.
    await startSession($)
    await w.clock.advance(5_000)
    expect(w.toasts).toHaveLength(1)
  })

  test('nothing is shown when auto-upgrade is disabled or nothing needs a look', async ($, on) => {
    let doc = status({ auto_upgrade: 'disabled', retirement_notices: [{ key: 'k', id: 'm' }] })
    const w = world(on, { proc: argv => (isSync(argv) ? { stdout: doc } : { exitCode: 2 }) })
    await startSession($)
    await w.clock.advance(5_000)
    doc = status({ deferred: { k: { id: 'm', reason: 'quota', expired: false } } })
    await startSession($)
    await w.clock.advance(5_000)
    expect(w.runs.filter(isSync)).toHaveLength(2)
    expect(w.toasts).toEqual([])
  })

  test('/router-sync reads status again and draws it', async ($, on) => {
    const w = world(on, {
      proc: argv => (isSync(argv) ? {
        stdout: status({
          retirement_notices: [{ key: 'openai_reasoning', id: 'model-sol', retirement_at: '2026-12-01', upgrade_to: null }],
          candidates: { claude_senior: 'model-opus-next' },
        }),
      } : { exitCode: 2 }),
    })
    await startSession($)
    const ran = await $.command.run(command('router-sync', ''))
    expect(ran.text).toBe('model-sync status read.')
    expect(w.opened).toEqual(['router-sync'])
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({
        plugin: PLUGIN, surface, component: 'Pane', requestId: 'router-sync',
        props: paneProps('Router model-sync'),
      })
      expect(await ui.find({ type: 'Text', text: 'openai_reasoning model-sol retires 2026-12-01' })).toBeDefined()
      expect(await ui.find({ type: 'Text', text: 'claude_senior → model-opus-next' })).toBeDefined()
      await ui.unmount()
    }
  })

  test('a failed status reading is shown in the pane, never toasted', async ($, on) => {
    const w = world(on, { proc: () => ({ exitCode: 1, stderr: 'Traceback ...\nModuleNotFoundError: No module named yaml' }) })
    await startSession($)
    await w.clock.advance(5_000)
    expect(w.toasts).toEqual([])
    await $.command.run(command('router-sync', ''))
    const ui = await $.ui.mount({
      plugin: PLUGIN, surface: 'terminal', component: 'Pane', requestId: 'router-sync',
      props: paneProps('Router model-sync'),
    })
    expect(await ui.find({ type: 'Text', text: /No module named yaml/ })).toBeDefined()
    await ui.unmount()
  })
})
