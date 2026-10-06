// Best-effort reading of a Bash command string. The mod never runs what it
// reads: it only looks for `dispatch_agent.py run` and `route_task.py`
// invocations. Anything it cannot read for certain (a variable it cannot
// resolve, a command substitution, a heredoc body) is left untracked.

/**
 * A parameter expansion is kept in the word as NUL + name + NUL, so a literal
 * `$` from single quotes is never confused with one. A shell command cannot
 * carry a NUL byte, so the marker cannot be forged by the command itself.
 */
const MARK = '\u0000'
const COMPLEX = `${MARK}?${MARK}`

export type Word = {
  /** The word after quote removal, expansions kept as markers. */
  text: string
}

export type Segment = {
  words: Word[]
  /** Ended by a lone `&`: the shell does not wait for it. */
  background: boolean
  /** Subshell depth (`(` … `)`) the segment starts at. */
  depth: number
  /** Feeds a pipe (`|`), or is fed by one: either way it runs in a subshell. */
  piped: boolean
}

const NAME_START = /[A-Za-z_]/
const NAME_CHAR = /[A-Za-z0-9_]/
const SPECIAL_PARAM = /[0-9?#@*!$-]/

/** Splits a command into simple commands (`;`, `&&`, `||`, `|`, `&`, newline, parentheses). */
export function segments(command: string): Segment[] {
  const out: Segment[] = []
  let words: Word[] = []
  let text = ''
  let inWord = false
  let skipNext = false
  let depth = 0
  let segDepth = 0
  let fedByPipe = false
  const heredocs: { delimiter: string; stripTabs: boolean }[] = []
  let i = 0
  const n = command.length

  const endWord = () => {
    if (inWord) {
      if (skipNext) skipNext = false
      else words.push({ text })
    }
    text = ''
    inWord = false
  }
  const endSegment = (background = false, pipe = false) => {
    endWord()
    skipNext = false
    if (words.length > 0) out.push({ words, background, depth: segDepth, piped: pipe || fedByPipe })
    // `( … ) &`: the `&` follows the closing parenthesis, after the last inner segment.
    else if (background && out.length > 0) out[out.length - 1]!.background = true
    words = []
    segDepth = depth
    fedByPipe = pipe
  }
  const takeHeredocs = () => {
    // `i` sits just past a newline: drop each pending body up to its delimiter line.
    for (const doc of heredocs.splice(0)) {
      while (i < n) {
        const eol = command.indexOf('\n', i)
        const end = eol === -1 ? n : eol
        let line = command.slice(i, end)
        if (doc.stripTabs) line = line.replace(/^\t+/, '')
        i = end + 1
        if (line === doc.delimiter) break
      }
    }
  }
  /** Reads a balanced `$( … )` / `( … )` body from `i` (just past the opening paren). */
  const skipBalanced = (open: string, close: string) => {
    let level = 1
    while (i < n && level > 0) {
      const c = command[i]
      if (c === '\\') { i += 2; continue }
      if (c === "'") { const j = command.indexOf("'", i + 1); i = j === -1 ? n : j + 1; continue }
      if (c === '"') {
        i += 1
        while (i < n && command[i] !== '"') i += command[i] === '\\' ? 2 : 1
        i += 1
        continue
      }
      if (c === open) level += 1
      else if (c === close) level -= 1
      i += 1
    }
  }
  const readExpansion = () => {
    // `i` is at `$`.
    const next = command[i + 1]
    if (next === "'" || next === '"') {
      // $'…' (ANSI-C escapes) and $"…" (locale translation): not read here.
      let j = i + 2
      while (j < n && command[j] !== next) j += command[j] === '\\' ? 2 : 1
      i = j + 1
      text += COMPLEX
      return
    }
    if (next === '(') {
      i += 2
      skipBalanced('(', ')')
      text += COMPLEX
      return
    }
    if (next === '{') {
      const close = command.indexOf('}', i + 2)
      const body = close === -1 ? '' : command.slice(i + 2, close)
      i = close === -1 ? n : close + 1
      text += /^[A-Za-z_][A-Za-z0-9_]*$/.test(body) ? `${MARK}${body}${MARK}` : COMPLEX
      return
    }
    if (next !== undefined && NAME_START.test(next)) {
      let j = i + 1
      while (j < n && NAME_CHAR.test(command[j]!)) j += 1
      text += `${MARK}${command.slice(i + 1, j)}${MARK}`
      i = j
      return
    }
    if (next !== undefined && SPECIAL_PARAM.test(next)) {
      text += COMPLEX
      i += 2
      return
    }
    text += '$'
    i += 1
  }

  while (i < n) {
    const c = command[i]!
    if (c === '\\') {
      const d = command[i + 1]
      if (d === '\n') { i += 2; continue }
      if (d !== undefined) { text += d; inWord = true }
      i += 2
      continue
    }
    if (c === "'") {
      const j = command.indexOf("'", i + 1)
      text += command.slice(i + 1, j === -1 ? n : j)
      inWord = true
      i = j === -1 ? n : j + 1
      continue
    }
    if (c === '"') {
      inWord = true
      i += 1
      while (i < n && command[i] !== '"') {
        const d = command[i]!
        if (d === '\\' && i + 1 < n && '"\\$`\n'.includes(command[i + 1]!)) {
          if (command[i + 1] !== '\n') text += command[i + 1]
          i += 2
        } else if (d === '$') {
          readExpansion()
        } else if (d === '`') {
          const j = command.indexOf('`', i + 1)
          i = j === -1 ? n : j + 1
          text += COMPLEX
        } else {
          text += d
          i += 1
        }
      }
      i += 1
      continue
    }
    if (c === '$') { inWord = true; readExpansion(); continue }
    if (c === '`') {
      const j = command.indexOf('`', i + 1)
      i = j === -1 ? n : j + 1
      text += COMPLEX
      inWord = true
      continue
    }
    if (c === '~' && !inWord) {
      const d = command[i + 1]
      if (d === undefined || d === '/' || /\s/.test(d) || ';&|)'.includes(d)) {
        text += `${MARK}HOME${MARK}`
      } else {
        text += COMPLEX
      }
      inWord = true
      i += 1
      continue
    }
    if (c === '#' && !inWord) {
      const eol = command.indexOf('\n', i)
      i = eol === -1 ? n : eol
      continue
    }
    if (c === '\n') {
      endSegment()
      i += 1
      takeHeredocs()
      continue
    }
    if (c === ' ' || c === '\t' || c === '\r') { endWord(); i += 1; continue }
    if (c === ';') { endSegment(); i += command[i + 1] === ';' ? 2 : 1; continue }
    if (c === '(' && inWord && /^[A-Za-z_][A-Za-z0-9_]*\+?=$/.test(text)) {
      // An array literal (`CMD=( … )`) holds words, not a command to run.
      i += 1
      skipBalanced('(', ')')
      text += COMPLEX
      continue
    }
    if (c === '(' || c === ')') {
      endSegment()
      depth = Math.max(0, depth + (c === '(' ? 1 : -1))
      segDepth = depth
      i += 1
      continue
    }
    if (c === '|') {
      const or = command[i + 1] === '|'
      endSegment(false, !or)
      i += or || command[i + 1] === '&' ? 2 : 1
      continue
    }
    if (c === '&') {
      const d = command[i + 1]
      if (d === '&') { endSegment(); i += 2; continue }
      if (d === '>') {
        // `&>` / `&>>`: a redirection, its target the next word.
        endWord()
        i += command[i + 2] === '>' ? 3 : 2
        skipNext = true
        continue
      }
      endSegment(true)
      i += 1
      continue
    }
    if (c === '>' || c === '<') {
      // An fd number glued to the operator (`2>`) is part of it, not a word.
      if (inWord && /^[0-9]+$/.test(text)) { text = ''; inWord = false }
      endWord()
      if (c === '<' && command.startsWith('<<<', i)) {
        i += 3
        skipNext = true
        continue
      }
      if (c === '<' && command[i + 1] === '<') {
        const stripTabs = command[i + 2] === '-'
        i += stripTabs ? 3 : 2
        while (i < n && (command[i] === ' ' || command[i] === '\t')) i += 1
        let delimiter = ''
        while (i < n && !/[\s;&|<>()]/.test(command[i]!)) {
          const d = command[i]!
          if (d === "'" || d === '"') {
            const j = command.indexOf(d, i + 1)
            delimiter += command.slice(i + 1, j === -1 ? n : j)
            i = j === -1 ? n : j + 1
          } else if (d === '\\') {
            delimiter += command[i + 1] ?? ''
            i += 2
          } else {
            delimiter += d
            i += 1
          }
        }
        heredocs.push({ delimiter, stripTabs })
        continue
      }
      i += 1
      if (command[i] === '>' || command[i] === '&' || command[i] === '|') i += 1
      skipNext = true
      continue
    }
    if (c === '*' || c === '?' || c === '[') {
      // A pathname pattern: the shell may expand it to something else.
      text += COMPLEX
      inWord = true
      i += 1
      continue
    }
    if (c === '{' && !(inWord === false && /[\s;]/.test(command[i + 1] ?? ' '))) {
      // Brace expansion (`r{1,2}`); a lone `{` opening a group stays a word.
      text += COMPLEX
      inWord = true
      i += 1
      continue
    }
    text += c
    inWord = true
    i += 1
  }
  endSegment()
  return out
}

/** True when the word holds an expansion this module cannot resolve. */
export function isOpaque(text: string, resolvable: readonly string[] = []): boolean {
  const re = new RegExp(`${MARK}([^${MARK}]*)${MARK}`, 'g')
  for (const m of text.matchAll(re)) {
    if (!resolvable.includes(m[1]!)) return true
  }
  return false
}

/** Replaces `$HOME`/`~` markers; undefined when anything else stays unresolved. */
export function resolveWord(text: string, home: string | undefined): string | undefined {
  if (isOpaque(text, home === undefined ? [] : ['HOME'])) return undefined
  return text.split(`${MARK}HOME${MARK}`).join(home ?? '')
}

/** The basename of a word, expansion markers dropped. */
export function basename(text: string): string {
  const plain = text.replace(new RegExp(`${MARK}[^${MARK}]*${MARK}`, 'g'), '')
  const slash = plain.lastIndexOf('/')
  return slash === -1 ? plain : plain.slice(slash + 1)
}

/** `dispatch_agent.py run` options that take no value; every other `--` option takes one. */
const RUN_FLAGS = new Set(['--require-artifact-allow-unchanged', '--require-single-linked-cwd'])

const ATTEMPT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

export type DispatchRun = {
  attemptId: string
  /** As written; resolved against the shell's directory by the caller when relative. */
  receiptDir: string
  seat: string | null
  runtime: string | null
  modelId: string | null
  deadlineSeconds: number | null
  fingerprint: string | null
  /** The child CLI's basename, from the argv after `--`. */
  cli: string | null
  childArgv: string[]
  /** Words before `dispatch_agent.py` in the same simple command (`caffeinate -i python3`). */
  wrappers: string[]
  background: boolean
}

const valueOf = (text: string | undefined, home: string | undefined) =>
  text === undefined ? undefined : resolveWord(text, home)

/**
 * Every `dispatch_agent.py run` in the command whose attempt id and receipt
 * directory can be read for certain. A run with an unreadable one is skipped.
 */
export function findDispatchRuns(command: string, homeDir?: string): DispatchRun[] {
  const runs: DispatchRun[] = []
  // A function definition's body runs only when called, perhaps never: a command
  // that defines one is not read at all (review i2).
  if (/(^|[\s;&|(])(function\s+[A-Za-z_][\w-]*|[A-Za-z_][\w-]*\s*\(\s*\))\s*[{(]/.test(command)) return runs
  // Whether the shell may have left its starting directory before this point.
  // Following `cd` through conditions, groups, subshells, background lists and
  // symbolic links is a shell's job, not this reader's: after any directory
  // change a relative receipt directory is left untracked (review i2).
  let moved = false
  const all = segments(command)
  // A list ended by `&` anywhere (`( … ) &`, `run | tee log &`, `{ …; } &`) may
  // hold the dispatch; the reader cannot tell which, so none counts as waited for.
  const anyBackground = all.some(seg => seg.background)
  // A command that reassigns HOME (`HOME=/x python3 …`, `export HOME=…`) makes `~` unknowable.
  const home = all.some(seg => seg.words.some(w => /^HOME=/.test(w.text))) ? undefined : homeDir
  for (const seg of all) {
    const words = seg.words.map(w => w.text)
    const lead = words.findIndex(w => !KEYWORDS.has(w) && !ASSIGNMENT.test(w))
    if (lead !== -1 && DIRECTORY_CHANGERS.has(basename(words[lead]!))) {
      moved = true
      continue
    }
    const at = words.findIndex(w => basename(w) === 'dispatch_agent.py')
    if (at === -1 || words[at + 1] !== 'run' || !isLauncher(words.slice(0, at))) continue
    const opts = new Map<string, string | undefined>()
    let k = at + 2
    let child: string[] = []
    while (k < words.length) {
      const w = words[k]!
      if (w === '--') { child = words.slice(k + 1); break }
      if (!w.startsWith('--')) { child = words.slice(k); break }
      const eq = w.indexOf('=')
      const name = eq === -1 ? w : w.slice(0, eq)
      if (RUN_FLAGS.has(name)) { k += 1; continue }
      if (eq !== -1) { opts.set(name, w.slice(eq + 1)); k += 1; continue }
      opts.set(name, words[k + 1])
      k += 2
    }
    const attemptId = valueOf(opts.get('--attempt-id'), undefined)
    const receiptDir = valueOf(opts.get('--receipt-dir'), home)
    if (attemptId === undefined || !ATTEMPT_ID.test(attemptId)) continue
    if (receiptDir === undefined || receiptDir === '') continue
    if (moved && !receiptDir.startsWith('/')) continue
    const deadline = Number(valueOf(opts.get('--deadline-seconds'), undefined))
    const plain = (name: string) => {
      const v = valueOf(opts.get(name), undefined)
      return v === undefined || v === '' ? null : v
    }
    const childPlain = child.map(w => resolveWord(w, home) ?? w.replaceAll(MARK, ''))
    runs.push({
      attemptId,
      receiptDir,
      seat: plain('--seat'),
      runtime: plain('--runtime'),
      modelId: plain('--model-id'),
      deadlineSeconds: Number.isFinite(deadline) && deadline > 0 ? deadline : null,
      fingerprint: plain('--decision-fingerprint'),
      cli: child.length > 0 ? basename(child[0]!) || null : null,
      childArgv: childPlain,
      wrappers: words.slice(0, at).map(basename),
      background: seg.background || anyBackground,
    })
  }
  return runs
}

/** Commands that may change the shell's directory (`eval`, `source` and `.` may run a `cd`). */
const DIRECTORY_CHANGERS = new Set(['cd', 'pushd', 'popd', 'builtin', 'eval', 'source', '.'])
/** Words that put the next word in command position. */
const KEYWORDS = new Set(['if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', 'time'])

/** Programs that run the rest of their arguments as a command. */
const WRAPPERS = new Set(['env', 'caffeinate', 'nohup', 'timeout', 'gtimeout', 'time', 'exec', 'nice', 'stdbuf'])
const PYTHON = /^python(\d+(\.\d+)?)?$/
/** Interpreter flags that still run the next word as a script (`-c` and `-m` do not). */
const PYTHON_FLAGS = /^-[BIsSuEOqvdb]+$/
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/
const NUMBER = /^\d+(\.\d+)?[smhd]?$/

/**
 * Whether the words in front of the script launch it: assignments, known
 * wrappers with their flags and durations, and a Python interpreter — nothing
 * else. `echo python3 dispatch_agent.py run …` or `ssh host python3 …` does not
 * run the supervisor here, so it is not tracked.
 */
export function isLauncher(prefix: readonly string[]): boolean {
  let sawPython = false
  for (const word of prefix) {
    const name = basename(word)
    if (sawPython) {
      if (!PYTHON_FLAGS.test(word)) return false
    } else if (PYTHON.test(name)) {
      sawPython = true
    } else if (!(ASSIGNMENT.test(word) || WRAPPERS.has(name) || word.startsWith('-') || NUMBER.test(word))) {
      return false
    }
  }
  return true
}

/** True when some simple command in it runs `route_task.py`. */
export function callsRouteTask(command: string): boolean {
  return segments(command).some(seg => seg.words.some(w => basename(w.text) === 'route_task.py'))
}

/**
 * Joins a possibly relative path onto a base directory. `.` and empty pieces
 * go; `..` stays, because folding it across a symbolic link names another
 * directory than the one the operating system resolves.
 */
export function joinPath(base: string, ...parts: string[]): string {
  let path = base
  for (const part of parts) path = part.startsWith('/') ? part : `${path}/${part}`
  const out = path.split('/').filter(piece => piece !== '' && piece !== '.')
  return `/${out.join('/')}`
}

/** Quotes one argument for a POSIX shell. */
export function shellQuote(value: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(value) ? value : `'${value.replaceAll("'", `'\\''`)}'`
}
