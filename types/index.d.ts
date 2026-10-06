// State contract of the deep-model-router Claude Code mod (hooks/mods/).
// The mod is a visibility layer only: nothing here feeds routing policy.

/** One `dispatch_agent.py run` attempt this session is watching. */
export type RouterSeat = {
  attemptId: string
  /** Absolute receipt directory. */
  receiptDir: string
  /** `--seat` as dispatched (reviewer-1, worker, judge, ...). */
  seat: string | null
  /** `--runtime`: the HOST runtime label, not the child CLI. */
  runtime: string | null
  /** `--model-id`, else the receipt's declared model id. */
  modelId: string | null
  /** The child CLI's basename (codex, grok, claude), read from the argv after `--`. */
  cli: string | null
  deadlineSeconds: number | null
  /** `--decision-fingerprint`, for the Verify command. */
  fingerprint: string | null
  /** How the attempt was registered. */
  source: 'command' | 'manual'
  /** When tracking started, ms since the epoch. */
  trackedAt: number
  /** When the dispatching Bash call returned; null while it still runs. */
  returnedAt: number | null
  /** The dispatching Bash call finished in the foreground (no receipt then means it never started). */
  commandDone: boolean
  /**
   * A receipt state, or one of the mod's own labels: PENDING (no receipt
   * yet), CLAIMED (claim sentinel, no receipt), NO_RECEIPT, INVALID_RECEIPT,
   * UNREADABLE.
   */
  state: string
  /** When `state` last changed, ms since the epoch. */
  stateSince: number
  startedAt: string | null
  deadlineAt: string | null
  finishedAt: string | null
  /** `status`'s liveness label while STARTING/RUNNING: supervised | orphaned | stale. */
  supervision: string | null
  processAlive: boolean | null
  verdict: string | null
  /** The last problem `status` reported, one line. */
  detail: string | null
  /** No further polling: a terminal state `status` confirmed, or a label that ends tracking. */
  final: boolean
  /** When `final` became true, ms since the epoch. */
  finalAt: number | null
  /** Consecutive `status` calls that could not be read. */
  failures: number
  /** When the current run of unreadable `status` calls began. */
  failingSince: number | null
  /** Attention labels already toasted for this attempt. */
  alerted: string[]
}

/** What `/router-sync` shows: the last `model_sync.py status` reading. */
export type RouterSyncReport = {
  at: number
  autoUpgrade: string
  retirements: { key: string; id: string; retirementAt: string | null; upgradeTo: string | null }[]
  deferred: { key: string; id: string; reason: string | null; expired: boolean }[]
  inFlight: { attemptId: string; receiptDir: string }[]
  candidates: { key: string; id: string }[]
  /** Set when the reading failed; the lists above are then empty. */
  error: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'deep-model-router': {
      seats: RouterSeat[]
      sync: RouterSyncReport | null
      /** One-time hints already shown this session (survives a hot reload). */
      shown: string[]
    }
  }
}
