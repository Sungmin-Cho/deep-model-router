// deep-model-router's Claude Code mod: a visibility layer over the router's
// scripts. It shows dispatched seats, warns when a route seats this session's
// own model, and surfaces model-sync notices. It never changes a route, a
// receipt or a review floor. The commands it runs on its own only read state
// (`dispatch_agent.py status`, `model_sync.py status`, `uname`); Cancel and the
// other buttons only fill the prompt.
//
// Claude Code only. Codex and Grok load hooks/hooks.json, which this module is
// not listed in; anything policy must enforce lives in the scripts instead.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RouterSeat, RouterSyncReport } from '../../types'
import { dispatchFindings, parseRoute, routeFindings } from './author'
import type { Finding } from './author'
import {
  SHOW_FINISHED_MS, announcements, applyStatus, attention, newSeat, remedy, sameAttempt, seatLabel, stateText,
  statusLine,
} from './seats'
import { callsRouteTask, findDispatchRuns, joinPath, resolveWord, segments, shellQuote } from './shell'
import { syncNotice, syncReport } from './sync'

type Engine = EngineInterface

const seatsAtom = atom({ plugin: 'deep-model-router', key: 'seats' } as const, [] as RouterSeat[])
const syncAtom = atom({ plugin: 'deep-model-router', key: 'sync' } as const, null as RouterSyncReport | null)
const shownAtom = atom({ plugin: 'deep-model-router', key: 'shown' } as const, [] as string[])

const SEATS_PANE = 'router-seats'
const SYNC_PANE = 'router-sync'
const POLL_MS = 20_000
/** When the model-sync notice is tried after a start: a surface may attach late (the desktop app). */
const SYNC_DELAYS_MS = [5_000, 30_000, 120_000]
const STATUS_TIMEOUT_MS = 20_000
const MAX_SEATS = 50
const TOAST_MS = 10_000

// Module variables start over on a hot reload; `session.start` fires again then.
let poller: { cancel: () => void } | undefined
let expiry: { cancel: () => void } | undefined
let polling = false
let generation = 0
let platform: Promise<string> | undefined
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

/**
 * One toast for everything one event has to say: the engine draws only a
 * plugin's newest toast, so several raised together would drop all but one.
 */
function toast($: Engine, texts: readonly string[]): void {
  if (texts.length > 0) $.ui.toast(texts.join(' · '), { timeoutMs: TOAST_MS })
}

/** Toasts the findings, each `once` key at most once per session. */
async function say($: Engine, findings: readonly Finding[]): Promise<void> {
  const once = findings.map(f => f.once).filter((k): k is string => k !== undefined)
  let fresh: string[] = []
  if (once.length > 0) {
    await update($, shownAtom, shown => {
      fresh = once.filter(k => !shown.includes(k))
      return fresh.length > 0 ? [...shown, ...fresh] : shown
    })
  }
  toast($, findings.filter(f => f.once === undefined || fresh.includes(f.once)).map(f => f.text))
}

async function runScript($: Engine, argv: string[]) {
  try {
    const r = await $.process.run(['python3', ...argv], { timeoutMs: STATUS_TIMEOUT_MS })
    return { exitCode: r.exitCode, stdout: r.stdout, stderr: r.stderr }
  } catch (err) {
    return { exitCode: -1, stdout: '', stderr: String(err) }
  }
}

/** `uname -s`, once per module load and never on a tool call's path. */
const detectPlatform = ($: Engine): Promise<string> =>
  (platform ??= $.process.run(['uname', '-s'], { timeoutMs: 5_000 }).then(r => r.stdout.trim(), () => ''))

/**
 * Redraws the status line and sets the timers from the seats as they stand:
 * the poll runs while any attempt is unfinished; otherwise one timer clears
 * finished seats off the line when their time is up (flagged ones stay until
 * cleared). Every change to the seats ends here, and only the newest call
 * decides, so a slower one cannot cancel a poll a newer seat needs.
 */
async function reconcile($: Engine): Promise<void> {
  const mine = ++generation
  const now = await $.clock.now()
  const seats = await read($, seatsAtom)
  if (mine !== generation) return
  $.ui.status(statusLine(seats, now))
  expiry?.cancel()
  expiry = undefined
  if (seats.some(s => !s.final)) {
    arm($)
    return
  }
  disarm()
  const due = seats
    .filter(s => s.finalAt !== null && attention(s, now) === undefined && now - s.finalAt < SHOW_FINISHED_MS)
    .map(s => s.finalAt! + SHOW_FINISHED_MS - now)
  if (due.length > 0) expiry = $.clock.after(Math.min(...due) + 1_000, () => background(reconcile($)))
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
      // The tool.call hook owns when the command returned, and may have set it
      // while this poll ran: the poll's older copy never overwrites it.
      const merged = current.map(s => {
        const r = readings.find(o => sameAttempt(o, s))
        return r === undefined ? s
          : { ...r, returnedAt: s.returnedAt ?? r.returnedAt, commandDone: r.commandDone || s.commandDone }
      })
      const out = announcements(current, merged, now)
      toasts = out.toasts
      return out.seats
    })
    toast($, toasts)
  } finally {
    polling = false
  }
  await reconcile($)
}

function arm($: Engine): void {
  if (poller !== undefined) return
  poller = $.clock.every(POLL_MS, () => { background(refresh($)) })
}

function disarm(): void {
  poller?.cancel()
  poller = undefined
}

/**
 * Adds the attempts not already tracked and returns the ones it added. A
 * tracked attempt keeps its record (and what was announced for it); only one
 * that never had a receipt is replaced, since a dispatch refused before spawn
 * may be fixed and run again under the same id. Past MAX_SEATS the oldest
 * finished records go; an unfinished one is never dropped.
 */
async function track($: Engine, seats: RouterSeat[]): Promise<{ added: RouterSeat[]; replaced: RouterSeat[] }> {
  const now = await $.clock.now()
  // One attempt may appear twice in one command (`run a || run a`).
  const unique = seats.filter((a, i) => seats.findIndex(b => sameAttempt(a, b)) === i)
  let added: RouterSeat[] = []
  let replaced: RouterSeat[] = []
  await update($, seatsAtom, list => {
    replaced = []
    added = unique.filter(a => {
      const old = list.find(s => sameAttempt(a, s))
      if (old !== undefined && old.final && old.state === 'NO_RECEIPT') replaced.push(old)
      return old === undefined || replaced.includes(old)
    })
    const kept = list.filter(s => !added.some(a => sameAttempt(a, s)))
    return capped([...kept, ...added], now)
  })
  await reconcile($)
  return { added, replaced }
}

/** Past MAX_SEATS: plain finished records go first, then flagged ones; unfinished never. */
function capped(all: RouterSeat[], now: number): RouterSeat[] {
  let excess = all.length - MAX_SEATS
  if (excess <= 0) return all
  const order = [...all.filter(s => s.final && attention(s, now) === undefined), ...all.filter(s => s.final && attention(s, now) !== undefined)]
  const drop = new Set(order.slice(0, excess))
  excess = 0
  return all.filter(s => !drop.has(s))
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
  if (notice !== undefined) await say($, [{ once: 'sync-notice', text: notice }])
  return report
}

// --- commands the pane buttons fill (never run) --------------------------------------

const cmd = ($: Engine, sub: string, args: string[]) =>
  `! python3 ${shellQuote(script($, 'dispatch_agent.py'))} ${sub} ${args.map(shellQuote).join(' ')}`

export const statusCommand = ($: Engine, s: RouterSeat) =>
  cmd($, 'status', ['--attempt-id', s.attemptId, '--receipt-dir', s.receiptDir])
export const cancelCommand = ($: Engine, s: RouterSeat) =>
  cmd($, 'cancel', ['--attempt-id', s.attemptId, '--receipt-dir', s.receiptDir])

/**
 * verify-evidence over the succeeded attempts that share this one's receipt
 * dir and decision, the expectations left to the person: the count, models
 * and fingerprint must come from the route, and filling them from the
 * receipts being checked would make the check pass by construction.
 */
export function verifyCommand($: Engine, seats: readonly RouterSeat[], s: RouterSeat): string {
  const group = seats.filter(o => o.state === 'SUCCEEDED' && o.receiptDir === s.receiptDir
    && o.fingerprint === s.fingerprint)
  return `${cmd($, 'verify-evidence', ['--receipt-dir', s.receiptDir, '--ids', group.map(o => o.attemptId).join(',')])} --expect-count `
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
    // A hot reload drops the module's timers, not the session's seats.
    await reconcile($)
    const trySync = (attempt: number): void => {
      $.clock.after(SYNC_DELAYS_MS[attempt]! - (SYNC_DELAYS_MS[attempt - 1] ?? 0), () => {
        background((async () => {
          if (await hasSurface($)) await syncCheck($, true)
          else if (attempt + 1 < SYNC_DELAYS_MS.length) trySync(attempt + 1)
        })())
      })
    }
    trySync(0)
    return next(e)
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const command = e.command
    if (!command.includes('dispatch_agent') && !command.includes('route_task')) return next(e)
    if (!(await hasSurface($))) return next(e)
    if (home === undefined) home = await $.env.get('HOME')
    const runs = findDispatchRuns(command, home)
    let added: RouterSeat[] = []
    let replaced: RouterSeat[] = []
    let seated: { run: (typeof runs)[number]; seat: RouterSeat }[] = []
    if (runs.length > 0) {
      const cwd = await $.session.cwd()
      const now = await $.clock.now()
      seated = runs.map(run => ({ run, seat: newSeat(run, joinPath(cwd, run.receiptDir), now, 'command') }))
      ;({ added, replaced } = await track($, seated.map(p => p.seat)))
      // Hints wait for nothing on the call's path; `uname` runs at most once.
      background((async () => {
        const os = await detectPlatform($)
        await say($, runs.flatMap(run => dispatchFindings(run, os)))
      })())
    }
    const ran = await next(e)
    if (added.length > 0) {
      if (ran.deny !== undefined) {
        // Refused before it ran: drop what this call added and put back what it replaced.
        await update($, seatsAtom, list => [
          ...list.filter(s => !added.some(a => sameAttempt(a, s))), ...replaced])
        await reconcile($)
      } else {
        const now = await $.clock.now()
        const backgrounded = e.run_in_background === true
          || (ran.isError !== true && ran.result?.backgroundTaskId !== undefined)
        // Matched by attempt AND directory: one id may be dispatched twice in one call.
        const isDone = (s: RouterSeat) => !backgrounded
          && seated.some(p => sameAttempt(p.seat, s) && !p.run.background)
        await update($, seatsAtom, list => list.map(s => (added.some(a => sameAttempt(a, s))
          ? { ...s, returnedAt: now, commandDone: s.commandDone || isDone(s) } : s)))
        background(refresh($))
      }
    }
    if (ran.deny === undefined && callsRouteTask(command)) {
      const route = routeOf(ran)
      if (route !== undefined) {
        await say($, routeFindings(route, await $.session.model()))
      }
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'router-seats' }, async ($, e) => {
    // Read like a shell line, so a directory with spaces can be quoted.
    const args = (segments(e.args)[0]?.words ?? []).map(w => w.text)
    if (args[0] === 'add') {
      const usage = { text: 'Usage: /router-seats add <receipt-dir> <attempt-id> (quote a directory with spaces)' }
      const dir = args.length === 3 ? resolveWord(args[1]!, await $.env.get('HOME')) : undefined
      const id = args[2]
      if (dir === undefined || dir === '' || id === undefined || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(id)) {
        return usage
      }
      const now = await $.clock.now()
      const seat = newSeat({ attemptId: id, seat: null, runtime: null, modelId: null, cli: null,
        deadlineSeconds: null, fingerprint: null }, joinPath(await $.session.cwd(), dir), now, 'manual')
      const { added } = await track($, [seat])
      background(refresh($))
      return { text: added.length > 0 ? `Tracking ${id} in ${seat.receiptDir}.` : `Already tracking ${id} in ${seat.receiptDir}.` }
    }
    if (args[0] === 'clear') {
      // A person asking to clear has seen what is flagged. A seat that may still
      // run stays; one that never wrote a receipt goes.
      let dropped = 0
      await update($, seatsAtom, list => {
        const kept = list.filter(s => !s.final && s.state !== 'NO_RECEIPT')
        dropped = list.length - kept.length
        return kept
      })
      await reconcile($)
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
        {seats.some(s => s.state === 'SUCCEEDED') && (
          <Text dimColor>Verify fills the ids; finish it from the route: --expect-count, --expect-fingerprint, --expect-models.</Text>
        )}
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
