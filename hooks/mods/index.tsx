// deep-model-router's Claude Code mod: a visibility layer over the router's
// scripts. It shows dispatched seats, warns when a route seats this session's
// own model, and surfaces model-sync notices. It never changes a route, a
// receipt or a review floor, and never runs a command a person did not press
// Enter on: Cancel and the rest only fill the prompt.
//
// Claude Code only. Codex and Grok load hooks/hooks.json, which this module is
// not listed in; anything policy must enforce lives in the scripts instead.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RouterSeat, RouterSyncReport } from '../../types'
import { dispatchFindings, parseRoute, routeFindings } from './author'
import type { Finding } from './author'
import {
  announcements, attention, newSeat, remedy, sameAttempt, seatLabel, stateText, statusLine, visibleSeats,
} from './seats'
import { applyStatus } from './seats'
import { callsRouteTask, findDispatchRuns, joinPath, shellQuote } from './shell'
import { syncNotice, syncReport } from './sync'

type Engine = EngineInterface

const seatsAtom = atom({ plugin: 'deep-model-router', key: 'seats' } as const, [] as RouterSeat[])
const syncAtom = atom({ plugin: 'deep-model-router', key: 'sync' } as const, null as RouterSyncReport | null)
const shownAtom = atom({ plugin: 'deep-model-router', key: 'shown' } as const, [] as string[])

const SEATS_PANE = 'router-seats'
const SYNC_PANE = 'router-sync'
const POLL_MS = 20_000
const SYNC_DELAY_MS = 5_000
const STATUS_TIMEOUT_MS = 20_000
const MAX_SEATS = 50
const TOAST_MS = 10_000

// Module variables start over on a hot reload; `session.start` fires again then.
let poller: { cancel: () => void } | undefined
let polling = false
let platform: string | undefined
let home: string | undefined

const script = ($: Engine, name: string) => `${$.plugin.root}/skills/model-router/scripts/${name}`

/**
 * Work started from a timer or after a hook returned: a failure there (a
 * process that would not start, an environment a reload tore down) is dropped,
 * never left as an unhandled rejection.
 */
const background = (work: Promise<unknown>): void => {
  work.catch(() => undefined)
}

const hasSurface = async ($: Engine) => (await $.session.surfaces()).length > 0

async function say($: Engine, finding: Finding): Promise<void> {
  if (finding.once !== undefined) {
    const key = finding.once
    let fresh = false
    await update($, shownAtom, shown => {
      fresh = !shown.includes(key)
      return fresh ? [...shown, key] : shown
    })
    if (!fresh) return
  }
  $.ui.toast(finding.text, { timeoutMs: TOAST_MS })
}

async function runScript($: Engine, argv: string[]) {
  try {
    const r = await $.process.run(['python3', ...argv], { timeoutMs: STATUS_TIMEOUT_MS })
    return { exitCode: r.exitCode, stdout: r.stdout, stderr: r.stderr }
  } catch (err) {
    return { exitCode: -1, stdout: '', stderr: String(err) }
  }
}

async function showStatus($: Engine): Promise<number> {
  const seats = await read($, seatsAtom)
  const now = await $.clock.now()
  $.ui.status(statusLine(seats, now))
  return visibleSeats(seats, now).length
}

/** Polls `dispatch_agent.py status` for every unfinished attempt and announces what changed. */
async function refresh($: Engine): Promise<void> {
  if (polling) return
  polling = true
  try {
    const now = await $.clock.now()
    const readings: RouterSeat[] = []
    for (const seat of (await read($, seatsAtom)).filter(s => !s.final)) {
      const reading = await runScript($, [script($, 'dispatch_agent.py'), 'status',
        '--attempt-id', seat.attemptId, '--receipt-dir', seat.receiptDir])
      readings.push(applyStatus(seat, reading, now))
    }
    let toasts: string[] = []
    await update($, seatsAtom, current => {
      // A tool.call hook may have marked the command done while this poll ran.
      const merged = current.map(s => {
        const r = readings.find(o => sameAttempt(o, s))
        return r === undefined ? s : { ...r, commandDone: r.commandDone || s.commandDone }
      })
      const out = announcements(current, merged, now)
      toasts = out.toasts
      return out.seats
    })
    for (const text of toasts) $.ui.toast(text, { timeoutMs: TOAST_MS })
    if ((await showStatus($)) === 0) disarm()
  } finally {
    polling = false
  }
}

function arm($: Engine): void {
  if (poller !== undefined) return
  poller = $.clock.every(POLL_MS, () => { background(refresh($)) })
}

function disarm(): void {
  poller?.cancel()
  poller = undefined
}

async function track($: Engine, seats: RouterSeat[]): Promise<void> {
  await update($, seatsAtom, list => {
    const kept = list.filter(s => !seats.some(a => sameAttempt(a, s)))
    return [...kept, ...seats].slice(-MAX_SEATS)
  })
  await showStatus($)
  arm($)
}

/** The route JSON a Bash call printed, read from stdout or (on a nonzero exit) the error text. */
function routeOf(ran: { result?: unknown; text?: string; isError?: true }): Record<string, unknown> | undefined {
  const texts: string[] = []
  const result = ran.result as { stdout?: unknown } | undefined
  if (typeof result?.stdout === 'string') texts.push(result.stdout)
  if (typeof ran.text === 'string') texts.push(ran.text)
  for (const text of texts) {
    const route = parseRoute(text) ?? parseRoute(text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1))
    if (route !== undefined) return route
  }
  return undefined
}

async function syncCheck($: Engine, toast: boolean): Promise<RouterSyncReport> {
  const r = await runScript($, [script($, 'model_sync.py'), 'status'])
  const report = syncReport(r.stdout, r.exitCode, r.stderr, await $.clock.now())
  await update($, syncAtom, () => report)
  const notice = toast ? syncNotice(report) : undefined
  if (notice !== undefined) await say($, { once: 'sync-notice', text: notice })
  return report
}

// --- commands the pane buttons fill (never run) --------------------------------------

const cmd = ($: Engine, sub: string, args: string[]) =>
  `! python3 ${shellQuote(script($, 'dispatch_agent.py'))} ${sub} ${args.map(shellQuote).join(' ')}`

export const statusCommand = ($: Engine, s: RouterSeat) =>
  cmd($, 'status', ['--attempt-id', s.attemptId, '--receipt-dir', s.receiptDir])
export const cancelCommand = ($: Engine, s: RouterSeat) =>
  cmd($, 'cancel', ['--attempt-id', s.attemptId, '--receipt-dir', s.receiptDir])

/** verify-evidence over the succeeded attempts that share this one's receipt dir and decision. */
export function verifyCommand($: Engine, seats: readonly RouterSeat[], s: RouterSeat): string {
  const group = seats.filter(o => o.state === 'SUCCEEDED' && o.receiptDir === s.receiptDir
    && o.fingerprint === s.fingerprint)
  const args = ['--receipt-dir', s.receiptDir, '--ids', group.map(o => o.attemptId).join(','),
    '--expect-count', String(group.length)]
  if (s.fingerprint !== null) args.push('--expect-fingerprint', s.fingerprint)
  if (group.every(o => o.modelId !== null)) args.push('--expect-models', group.map(o => o.modelId).join(','))
  return cmd($, 'verify-evidence', args)
}

const fill = ($: Engine, text: string) => $.prompt.fill({ text, mode: 'replace' })

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'router-seats',
      description: 'Router seats this session dispatched: state, liveness, and commands to act on them',
      argumentHint: '[add <receipt-dir> <attempt-id> | clear]',
    })
    await $.command.register({
      name: 'router-sync',
      description: 'model-sync status: retirement notices, deferred probes, probe runs in flight',
    })
    // A hot reload drops the module's timer, not the session's seats.
    if ((await showStatus($)) > 0) arm($)
    $.clock.after(SYNC_DELAY_MS, () => {
      background((async () => {
        if (await hasSurface($)) await syncCheck($, true)
      })())
    })
    return next(e)
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const command = e.command
    if (!command.includes('dispatch_agent') && !command.includes('route_task')) return next(e)
    if (!(await hasSurface($))) return next(e)
    if (home === undefined) home = await $.env.get('HOME')
    const runs = findDispatchRuns(command, home)
    let added: RouterSeat[] = []
    if (runs.length > 0) {
      if (platform === undefined) {
        const uname = await $.process.run(['uname', '-s']).catch(() => undefined)
        platform = uname?.stdout.trim() ?? ''
      }
      const cwd = await $.session.cwd()
      const now = await $.clock.now()
      added = runs.map(run => newSeat(run, joinPath(cwd, ...run.cds, run.receiptDir), now, 'command'))
      for (const run of runs) for (const f of dispatchFindings(run, platform)) await say($, f)
      await track($, added)
    }
    const ran = await next(e)
    if (added.length > 0) {
      if (ran.deny !== undefined) {
        // Refused before it ran: nothing to watch.
        await update($, seatsAtom, list => list.filter(s => !added.some(a => sameAttempt(a, s))))
        await showStatus($)
      } else {
        const backgrounded = e.run_in_background === true
          || (ran.isError !== true && ran.result?.backgroundTaskId !== undefined)
        const done = added.filter((_, i) => !backgrounded && !runs[i]!.background)
        if (done.length > 0) {
          await update($, seatsAtom, list =>
            list.map(s => (done.some(d => sameAttempt(d, s)) ? { ...s, commandDone: true } : s)))
        }
        background(refresh($))
      }
    }
    if (ran.deny === undefined && callsRouteTask(command)) {
      const route = routeOf(ran)
      if (route !== undefined) {
        for (const f of routeFindings(route, await $.session.model())) await say($, f)
      }
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'router-seats' }, async ($, e) => {
    const args = e.args.trim().split(/\s+/).filter(Boolean)
    if (args[0] === 'add') {
      const [, dir, id] = args
      if (dir === undefined || id === undefined || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(id)) {
        return { text: 'Usage: /router-seats add <receipt-dir> <attempt-id>' }
      }
      const now = await $.clock.now()
      const base = dir.startsWith('~/') ? `${await $.env.get('HOME') ?? ''}/${dir.slice(2)}` : dir
      const seat = newSeat({ attemptId: id, seat: null, runtime: null, modelId: null, cli: null,
        deadlineSeconds: null, fingerprint: null }, joinPath(await $.session.cwd(), base), now, 'manual')
      await track($, [seat])
      background(refresh($))
      return { text: `Tracking ${id} in ${seat.receiptDir}.` }
    }
    if (args[0] === 'clear') {
      // A person asking to clear has seen what is flagged; unfinished seats stay.
      let dropped = 0
      await update($, seatsAtom, list => {
        const kept = list.filter(s => !s.final)
        dropped = list.length - kept.length
        return kept
      })
      await showStatus($)
      return { text: `Cleared ${dropped} finished seat(s).` }
    }
    await $.ui.open({ id: SEATS_PANE, title: 'Router seats' })
    background(refresh($))
    const seats = await read($, seatsAtom)
    return { text: `Router seats: ${seats.length} tracked this session.` }
  })

  on('command.run', { command: 'router-sync' }, async $ => {
    await $.ui.open({ id: SYNC_PANE, title: 'Router model-sync' })
    const report = await syncCheck($, false)
    return { text: report.error !== null ? `model-sync status failed: ${report.error}` : 'model-sync status read.' }
  })

  on('ui.render', { component: 'Pane', requestId: SEATS_PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const seats = await read($, seatsAtom)
    const now = await $.clock.now()
    if (seats.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No dispatched seats tracked yet.</Text>
          <Text dimColor>A `dispatch_agent.py run` in a Bash call is picked up on its own; otherwise /router-seats add &lt;receipt-dir&gt; &lt;attempt-id&gt;.</Text>
        </Box>
      )
    }
    const room = Math.max(1, Math.floor(((e.viewport?.rows ?? 24) - 2) / 4))
    const rows = [...seats].reverse().slice(0, room)
    return (
      <Box flexDirection="column" gap={1}>
        {rows.map((s, i) => {
          const label = attention(s, now)
          const color = label !== undefined ? 'warning'
            : s.state === 'SUCCEEDED' ? 'success' : s.final ? 'error' : 'text'
          return (
            <Box key={`seat-${i}`} flexDirection="column">
              <Text bold>{s.seat ?? 'seat'} {seatLabel(s)} <Text color={color}>{stateText(s, now)}</Text></Text>
              <Text dimColor wrap="truncate-middle">
                {s.attemptId} · {s.receiptDir}{s.supervision !== null ? ` · ${s.supervision}` : ''}
              </Text>
              {label !== undefined && <Text color="warning">⚠ {label}: {remedy(label)}</Text>}
              {s.detail !== null && label === undefined && <Text dimColor>{s.detail}</Text>}
              <Box flexDirection="row" gap={1}>
                <Button key={`status-${i}`} label="Status" onPress={() => fill($, statusCommand($, s))} />
                {!s.final && <Button key={`cancel-${i}`} label="Cancel" onPress={() => fill($, cancelCommand($, s))} />}
                {s.state === 'SUCCEEDED' && (
                  <Button key={`verify-${i}`} label="Verify" onPress={() => fill($, verifyCommand($, seats, s))} />
                )}
              </Box>
            </Box>
          )
        })}
        {seats.length > rows.length && <Text dimColor>{seats.length - rows.length} older seat(s) not shown.</Text>}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: SYNC_PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const report = await read($, syncAtom)
    const refreshButton = <Button key="sync-refresh" label="Refresh" onPress={() => background(syncCheck($, false))} />
    if (report === null) {
      return <Box flexDirection="column"><Text dimColor>Reading model-sync status…</Text>{refreshButton}</Box>
    }
    if (report.error !== null) {
      return <Box flexDirection="column"><Text color="error">model-sync status failed: {report.error}</Text>{refreshButton}</Box>
    }
    return (
      <Box flexDirection="column" gap={1}>
        <Text>auto-upgrade: <Text bold>{report.autoUpgrade}</Text></Text>
        <Box flexDirection="column">
          <Text bold>Retirement notices ({report.retirements.length})</Text>
          {report.retirements.map((r, i) => (
            <Text key={`ret-${i}`}>{r.key} {r.id}{r.retirementAt !== null ? ` retires ${r.retirementAt}` : ''}{r.upgradeTo !== null ? ` → ${r.upgradeTo}` : ''}</Text>
          ))}
        </Box>
        <Box flexDirection="column">
          <Text bold>Deferred probes ({report.deferred.length})</Text>
          {report.deferred.map((d, i) => (
            <Text key={`def-${i}`} color={d.expired ? 'warning' : 'text'}>{d.key} {d.id} {d.reason ?? ''}{d.expired ? ' — due again' : ''}</Text>
          ))}
        </Box>
        <Box flexDirection="column">
          <Text bold>Probe runs in flight ({report.inFlight.length})</Text>
          {report.inFlight.map((r, i) => <Text key={`run-${i}`} dimColor>{r.attemptId} · {r.receiptDir}</Text>)}
        </Box>
        <Box flexDirection="column">
          <Text bold>Candidates ({report.candidates.length})</Text>
          {report.candidates.map((c, i) => <Text key={`cand-${i}`} dimColor>{c.key} → {c.id}</Text>)}
        </Box>
        {refreshButton}
      </Box>
    )
  })
}
