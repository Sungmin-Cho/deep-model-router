// The world beneath the mod in a test: every `$` call it makes is answered
// here, and what it showed (toasts, status lines, fills, panes, processes) is
// recorded for the test to read.

import type { On } from 'claude-code'
import { mock } from 'claude-code/testing'
import type { MockClock } from 'claude-code/testing'

export const PLUGIN = 'deep-model-router'
export const T0 = Date.parse('2026-10-06T00:00:00Z')

export type Proc = { exitCode?: number; stdout?: string; stderr?: string }
export type BashAnswer = Proc & { background?: boolean; isError?: boolean; deny?: string }

export type World = {
  clock: MockClock
  toasts: string[]
  statuses: (string | undefined)[]
  fills: string[]
  opened: string[]
  runs: string[][]
  bash: string[]
}

export type WorldOptions = {
  model?: string
  cwd?: string
  /** The surfaces drawing; a function when they change during the test. */
  surfaces?: ('terminal' | 'desktop')[] | (() => ('terminal' | 'desktop')[])
  platform?: string
  /** Answers `$.process.run` for everything but `uname`. */
  proc?: (argv: readonly string[]) => Proc
  /** Answers the Bash tool beneath the plugins (it may take its time, or refuse). */
  bash?: (command: string) => BashAnswer | Promise<BashAnswer>
}

export function world(on: On, options: WorldOptions = {}): World {
  const w: World = {
    clock: mock.clock(on, { now: T0 }),
    toasts: [], statuses: [], fills: [], opened: [], runs: [], bash: [],
  }
  mock.env(on, { HOME: '/home/me' })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.surfaces', () => {
    const surfaces = options.surfaces ?? ['terminal']
    return { value: typeof surfaces === 'function' ? surfaces() : surfaces }
  })
  on('session.cwd', () => ({ value: options.cwd ?? '/work' }))
  on('session.model', () => ({ value: options.model ?? 'model-opus[1m]' }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.toast', ($, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    w.statuses.push(e.text)
    return { value: undefined }
  })
  on('prompt.fill', ($, e) => {
    w.fills.push(e.text)
    return { isFilled: true }
  })
  on('ui.open', ($, e) => {
    w.opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('process.run', ($, e) => {
    w.runs.push([...e.argv])
    const p: Proc = e.argv[0] === 'uname'
      ? { stdout: `${options.platform ?? 'Darwin'}\n` }
      : options.proc?.(e.argv) ?? { exitCode: 2, stderr: 'no answer in this test' }
    return {
      value: {
        exitCode: p.exitCode ?? 0, stdout: p.stdout ?? '', stderr: p.stderr ?? '',
        isStdoutTruncated: false, isStderrTruncated: false,
      },
    }
  })
  on('tool.call', { tool: 'Bash' }, async ($, e) => {
    w.bash.push(e.command)
    const p = (await options.bash?.(e.command)) ?? {}
    if (p.deny !== undefined) return { deny: p.deny }
    const stdout = p.stdout ?? ''
    if (p.isError === true) return { isError: true as const, result: stdout, text: `Exit code ${p.exitCode ?? 1}\n${stdout}` }
    return {
      result: {
        stdout, stderr: p.stderr ?? '', interrupted: false,
        ...(p.background === true ? { backgroundTaskId: 'bg-1' } : {}),
      },
      text: stdout,
    }
  })
  return w
}

/** The `status` reading for an attempt, as `dispatch_agent.py status` prints it. */
export function receipt(attemptId: string, state: string, extra: {
  verdict?: string; supervision?: string; processAlive?: boolean; startedAt?: string; deadlineAt?: string
  finishedAt?: string; modelId?: string; seat?: string
} = {}): Proc {
  const doc: Record<string, unknown> = {
    attempt_id: attemptId, seat: extra.seat ?? 'reviewer-1', runtime: 'claude_code',
    model_id: extra.modelId ?? 'model-sol', decision_fingerprint: null,
    timing: {
      started_at: extra.startedAt ?? '2026-10-06T00:00:00Z', deadline_at: extra.deadlineAt ?? '2026-10-06T00:20:00Z',
      finished_at: extra.finishedAt ?? null, launch_anchor_at: null,
    },
    result: { state, verdict: extra.verdict ?? null },
  }
  if (extra.supervision !== undefined) doc['supervision'] = extra.supervision
  if (extra.processAlive !== undefined) doc['process_alive'] = extra.processAlive
  return { stdout: JSON.stringify(doc, null, 2) }
}

export const DISPATCH = [
  'caffeinate -i python3 "$SKILL_DIR"/scripts/dispatch_agent.py run',
  '--attempt-id r1-sol --receipt-dir ~/receipts',
  '--deadline-seconds 1200 --seat reviewer-1 --runtime claude_code --model-id model-sol',
  '--prompt-file r1.txt --output-schema review',
  '-- codex exec -m model-sol -s read-only -',
].join(' \\\n  ')

export const startSession = ($: { session: { start: (e: { cwd: string; surface: 'terminal'; isInteractive: boolean }) => Promise<unknown> } }) =>
  $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

/** A slash command as the person types it at the prompt. */
export const command = (name: string, args = '') => ({
  command: name, args, origin: { kind: 'composer' as const },
  presentation: { isFullscreen: true, columns: 120 },
})

/** A docked pane's props, as the engine hands them to the drawing. */
export const paneProps = (title: string) => ({
  title, isFocused: true, bodyColumns: 100, placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 38 }, view: {},
})
