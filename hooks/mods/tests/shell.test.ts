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

  test('route_task.py calls are found in any simple command', () => {
    expect(callsRouteTask('cd x && python3 "$SKILL_DIR"/scripts/route_task.py --format json')).toBe(true)
    expect(callsRouteTask('grep route_task README.md')).toBe(false)
  })

  test('paths and quoting', () => {
    expect(joinPath('/a/b', 'c', '../d', './e')).toBe('/a/b/d/e')
    expect(joinPath('/a', '/abs', 'x')).toBe('/abs/x')
    expect(shellQuote('/plain/path-1.json')).toBe('/plain/path-1.json')
    expect(shellQuote("it's here")).toBe("'it'\\''s here'")
  })
})
