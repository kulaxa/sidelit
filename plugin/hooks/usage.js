// The plan's usage limits, for the bars at the bottom of Sidelit's sidebar.
//
// Claude Code measures them itself after each turn, the figures its status line shows
// (session.measure, Claude Code 2.1.287 or later). This writes them where the sidebar reads
// them, $XDG_RUNTIME_DIR/sidelit/usage.json: for each window (five_hour, seven_day, a gateway's
// spend_limit) how much is used and when it resets. Nothing else: no login, no token, no request
// of its own. Off a subscription there are no windows and nothing is written.

export function register(on) {
  on('session.measure', async ($, e, next) => {
    if (e.changed.includes('rateLimits') && e.rateLimits.length) {
      const home = await $.env.get('SIDELIT_HOME')  // a test instance's folder (same rule as hook.py)
      const runtime = (await $.env.get('XDG_RUNTIME_DIR')) || '/tmp'
      const limits = e.rateLimits.map(({ kind, percentUsed, resetsAt }) => ({ kind, percentUsed, resetsAt }))
      await $.fs.write(`${home || runtime + '/sidelit'}/usage.json`,
                       JSON.stringify({ at: await $.clock.now(), limits }) + '\n')
    }
    return next(e)
  }).catch(($, e, next) => next(e))  // (never in Claude's way: the bars just wait for the next turn)
}
