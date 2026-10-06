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
    expect(findDispatchRuns(`command -v python3 ${tail}`)).toEqual([])
    expect(findDispatchRuns(`python3 -c ${tail}`)).toEqual([])
    expect(findDispatchRuns(`python3 -m ${tail}`)).toEqual([])
    expect(findDispatchRuns(`python3 -I -u ${tail}`)).toHaveLength(1)
    expect(findDispatchRuns(`timeout 3600 python3 -u ${tail}`)).toHaveLength(1)
    expect(findDispatchRuns(`./${tail}`)).toHaveLength(1)
    expect(findDispatchRuns(`FOO=1 nohup python3.14 ${tail} &`)).toHaveLength(1)
  })

  test('after any directory change, a relative receipt dir is left untracked (review i2)', () => {
    const run = 'python3 dispatch_agent.py run --attempt-id a --receipt-dir r -- x'
    for (const command of [
      `cd sub && ${run}`, `(cd /other); ${run}`, `true; cd /other && ${run}`, `cd /other & ${run}`, `cd /other && true & ${run}`,
      `pushd sub && ${run}`, `if false; then :; cd /other; fi; ${run}`, `cd /var; cd ..; ${run}`,
      `source env.sh; ${run}`, `. ./env.sh && ${run}`, `eval "$SETUP"; ${run}`, `builtin cd x; ${run}`,
      `command cd /tmp; ${run}`,
    ]) {
      expect(findDispatchRuns(command), command).toEqual([])
    }
    // An absolute receipt directory does not depend on any of that.
    expect(findDispatchRuns(`cd /other && ${run.replace('--receipt-dir r', '--receipt-dir /abs')}`)).toHaveLength(1)
    // The common opening `cd /absolute && …` is followed (review i3); not with `..`, a later cd, or a background list.
    expect(findDispatchRuns(`cd /abs/repo && ${run}`)[0]!.receiptDir).toBe('/abs/repo/r')
    expect(findDispatchRuns(`cd ~/repo && ${run}`, '/h')[0]!.receiptDir).toBe('/h/repo/r')
    for (const command of [`cd /abs/../x && ${run}`, `cd /abs && cd sub && ${run}`, `cd /abs && ${run} &`, `cd rel && ${run}`, `chdir /abs; ${run}`]) {
      expect(findDispatchRuns(command), command).toEqual([])
    }
    expect(findDispatchRuns(`cd /abs; ns.f() { ${run}; }`)).toEqual([])
    // A directory change after the dispatch, or a path that merely ends in cd, changes nothing.
    expect(findDispatchRuns(`${run}; cd /other`)).toHaveLength(1)
    expect(findDispatchRuns(run.replace('-- x', '--child-cwd /w/cd -- x'))).toHaveLength(1)
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

  test('an array literal or a function body is not a dispatch (review i2)', () => {
    const tail = 'python3 dispatch_agent.py run --attempt-id a --receipt-dir /abs -- codex exec -'
    expect(findDispatchRuns(`CMD=(${tail}); echo defined`)).toEqual([])
    expect(findDispatchRuns(`CMD+=(${tail})`)).toEqual([])
    expect(findDispatchRuns(`d() { echo; ${tail}; }`)).toEqual([])
    expect(findDispatchRuns(`function d { ${tail}; }`)).toEqual([])
    // Nested quotes inside an array literal, a compound function body (review i3).
    expect(findDispatchRuns(`CMD=("$(echo ")")" ${tail}); echo defined`)).toEqual([])
    expect(findDispatchRuns(`f()\nif true; then\n${tail}\nfi`)).toEqual([])
    // Function-like text in quotes or a heredoc is data, not a definition (review i3).
    expect(findDispatchRuns("python3 dispatch_agent.py run --attempt-id a --receipt-dir /abs -- claude -p 'Review function f() { return 1; }'")).toHaveLength(1)
    expect(findDispatchRuns(`cat <<'EOF' > p.txt\nf() { :; }\nEOF\n${tail}`)).toHaveLength(1)
    // A grouped or subshelled dispatch backgrounded as a whole is not waited for.
    expect(findDispatchRuns(`( ${tail} ) &`)[0]!.background).toBe(true)
    expect(findDispatchRuns(`${tail} 2>&1 | tee log &`)[0]!.background).toBe(true)
    expect(findDispatchRuns(`${tail} 2>&1 | tee log`)[0]!.background).toBe(false)
  })

  test('the round-4 reproductions (both reviewers)', () => {
    const run = 'python3 dispatch_agent.py run --attempt-id a --receipt-dir /abs -- x'
    // Function definitions with a space before `()`, and `function` after a keyword.
    for (const command of [
      `f () {\n  ${run}\n}`, `f () { :; ${run}; }`, `f ( )\n{ ${run}; }`, `if true; then function f { :; ${run}; }; fi`,
    ]) expect(findDispatchRuns(command), command).toEqual([])
    // The opening cd counts only along an unbroken && chain to the dispatch.
    const rel = run.replace('--receipt-dir /abs', '--receipt-dir receipts')
    for (const command of [
      `cd /missing || ${rel}`, `cd /missing && true; ${rel}`, `cd /abs; ${rel}`, `cd /abs && true || ${rel}`,
    ]) expect(findDispatchRuns(command), command).toEqual([])
    expect(findDispatchRuns(`cd /abs && true && ${rel}`)[0]!.receiptDir).toBe('/abs/receipts')
    // `command -v` looks up; `command cd` moves.
    expect(findDispatchRuns(`command -v codex >/dev/null && ${rel}`)[0]!.receiptDir).toBe('receipts')
    expect(findDispatchRuns(`cd /abs && command -v codex && ${rel}`)[0]!.receiptDir).toBe('/abs/receipts')
    expect(findDispatchRuns(`command cd /tmp; ${rel}`)).toEqual([])
    expect(findDispatchRuns(`builtin cd /tmp && ${rel}`)).toEqual([])
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
