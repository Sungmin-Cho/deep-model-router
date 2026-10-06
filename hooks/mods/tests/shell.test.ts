import { describe, expect, test } from 'claude-code/testing'

import { callsRouteTask, findDispatchRuns, joinPath, segments, shellQuote } from '../shell'

const words = (command: string) => segments(command).map(s => s.words.map(w => w.text.replaceAll('\u0000', '$')))

describe('shell reading', () => {
  test('quotes, escapes, operators and redirections', () => {
    expect(words(`a 'b c' "d \\"e\\"" f\\ g && h|i; j > out 2>&1 < in &`)).toEqual([
      ['a', 'b c', 'd "e"', 'f g'], ['h'], ['i'], ['j'],
    ])
    expect(segments('x &').map(s => s.background)).toEqual([true])
    expect(words('a # comment\nb')).toEqual([['a'], ['b']])
    expect(words('a \\\n b')).toEqual([['a', 'b']])
  })

  test('heredoc bodies are skipped, here-strings too', () => {
    expect(words("cat <<'EOF' > f\nrun me\nEOF\nnext")).toEqual([['cat'], ['next']])
    expect(words('cat <<-EOF\n\tbody\n\tEOF\nnext')).toEqual([['cat'], ['next']])
    expect(words('grep x <<< "$v" ; y')).toEqual([['grep', 'x'], ['y']])
  })

  test('a dispatch run is read whatever wraps it', () => {
    const [run] = findDispatchRuns(
      'env A=1 caffeinate -i nohup python3 "$SKILL_DIR/scripts/dispatch_agent.py" run --attempt-id=r1 '
      + '--receipt-dir "$HOME/rc" --require-single-linked-cwd --seat reviewer-2 --deadline-seconds 900 '
      + '--decision-fingerprint ab12 -- grok --no-auto-update -m grok-model --prompt-file /dev/stdin > log 2>&1 &',
      '/home/me',
    )
    expect(run).toMatchObject({
      attemptId: 'r1', receiptDir: '/home/me/rc', seat: 'reviewer-2', deadlineSeconds: 900,
      fingerprint: 'ab12', cli: 'grok', background: true,
    })
    expect(run!.wrappers).toEqual(['env', 'A=1', 'caffeinate', '-i', 'nohup', 'python3'])
    expect(run!.childArgv.slice(0, 3)).toEqual(['grok', '--no-auto-update', '-m'])
  })

  test('an id or directory it cannot resolve leaves the run out', () => {
    expect(findDispatchRuns('python3 dispatch_agent.py run --attempt-id $A --receipt-dir r -- x')).toEqual([])
    expect(findDispatchRuns('python3 dispatch_agent.py run --attempt-id a --receipt-dir `pwd` -- x')).toEqual([])
    expect(findDispatchRuns('python3 dispatch_agent.py run --attempt-id ../a --receipt-dir r -- x')).toEqual([])
    expect(findDispatchRuns('python3 dispatch_agent.py run --receipt-dir r -- x')).toEqual([])
    expect(findDispatchRuns('cd - && python3 dispatch_agent.py run --attempt-id a --receipt-dir r -- x')).toEqual([])
    expect(findDispatchRuns("echo 'python3 dispatch_agent.py run --attempt-id a --receipt-dir r'")).toEqual([])
    // An absolute directory survives an unknown cd.
    expect(findDispatchRuns('cd "$X" && python3 dispatch_agent.py run --attempt-id a --receipt-dir /r -- x')).toHaveLength(1)
  })

  test('~ and $HOME resolve; a quoted ~ stays literal', () => {
    expect(findDispatchRuns('python3 dispatch_agent.py run --attempt-id a --receipt-dir ~/r -- x', '/h')[0]!.receiptDir).toBe('/h/r')
    expect(findDispatchRuns('python3 dispatch_agent.py run --attempt-id a --receipt-dir ${HOME}/r -- x', '/h')[0]!.receiptDir).toBe('/h/r')
    expect(findDispatchRuns("python3 dispatch_agent.py run --attempt-id a --receipt-dir '~/r' -- x", '/h')[0]!.receiptDir).toBe('~/r')
  })

  test('only a command that launches the supervisor counts (review i1)', () => {
    const tail = 'dispatch_agent.py run --attempt-id a --receipt-dir /r -- codex exec -'
    expect(findDispatchRuns(`echo ${tail}`)).toEqual([])
    expect(findDispatchRuns(`echo python3 ${tail}`)).toEqual([])
    expect(findDispatchRuns(`ssh host python3 ${tail}`)).toEqual([])
    expect(findDispatchRuns(`grep -n ${tail}`)).toEqual([])
    expect(findDispatchRuns(`timeout 3600 python3 -u ${tail}`)).toHaveLength(1)
    expect(findDispatchRuns(`./${tail}`)).toHaveLength(1)
    expect(findDispatchRuns(`FOO=1 nohup python3.14 ${tail} &`)).toHaveLength(1)
  })

  test('a cd counts only where it moves the dispatching shell (review i1)', () => {
    const run = 'python3 dispatch_agent.py run --attempt-id a --receipt-dir r -- x'
    expect(findDispatchRuns(`cd sub && ${run}`)[0]!.cds).toEqual(['sub'])
    for (const command of [
      `(cd /other); ${run}`, `(cd /other && make) ; ${run}`, `cd /other & ${run}`, `cd /other | cat; ${run}`,
      `pushd sub && ${run}`, `if cd sub; then ${run}; fi`, `{ cd sub; ${run}; }`, `cd -P sub && ${run}`,
    ]) {
      expect(findDispatchRuns(command), command).toEqual([])
    }
    // An absolute receipt directory does not depend on any of that.
    expect(findDispatchRuns(`(cd /other); ${run.replace('--receipt-dir r', '--receipt-dir /abs')}`)).toHaveLength(1)
    // A path that merely ends in cd is no cd.
    expect(findDispatchRuns(`${run.replace('-- x', '--child-cwd /w/cd -- x')}`)).toHaveLength(1)
  })

  test('patterns, ANSI-C quotes and a reassigned HOME are opaque (review i1)', () => {
    const run = (dir: string) => `python3 dispatch_agent.py run --attempt-id a --receipt-dir ${dir} -- x`
    expect(findDispatchRuns(run('r*'), '/h')).toEqual([])
    expect(findDispatchRuns(run('r?'), '/h')).toEqual([])
    expect(findDispatchRuns(run('r[12]'), '/h')).toEqual([])
    expect(findDispatchRuns(run('r{1,2}'), '/h')).toEqual([])
    expect(findDispatchRuns(run("$'receipts'"), '/h')).toEqual([])
    expect(findDispatchRuns(run("'r*'"), '/h')[0]!.receiptDir).toBe('r*')
    expect(findDispatchRuns(`HOME=/other ${run('~/r')}`, '/h')).toEqual([])
    expect(findDispatchRuns(`export HOME=/other; ${run('$HOME/r')}`, '/h')).toEqual([])
    expect(findDispatchRuns(`{ ${run('/abs')}; }`, '/h')).toEqual([])
  })

  test('a command substitution with quotes inside it stays one word', () => {
    const [run] = findDispatchRuns('python3 dispatch_agent.py run --attempt-id a --receipt-dir /r --seat "$(echo ")")" -- x')
    expect(run).toMatchObject({ attemptId: 'a', receiptDir: '/r', seat: null })
  })

  test('route_task.py calls are found in any simple command', () => {
    expect(callsRouteTask('cd x && python3 "$SKILL_DIR"/scripts/route_task.py --format json')).toBe(true)
    expect(callsRouteTask('grep route_task README.md')).toBe(false)
  })

  test('paths and quoting', () => {
    expect(joinPath('/a/b', 'c', '../d', './e')).toBe('/a/b/c/../d/e')
    expect(joinPath('/a', '/abs', 'x')).toBe('/abs/x')
    expect(shellQuote('/plain/path-1.json')).toBe('/plain/path-1.json')
    expect(shellQuote("it's here")).toBe("'it'\\''s here'")
  })
})
