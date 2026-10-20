# Other sources

Sessions that don't run in a terminal on this desktop (a web app's agent jobs, a CI board,
another machine's agents) can show up too, as their own group under the workspaces. Add one
file per source to `~/.config/sidelit/sources/` (yours, never part of this repo):

```json
{
  "name": "My jobs",
  "url": "http://127.0.0.1:8080/feed",
  "token_file": "~/feed.token",
  "every": 10,
  "report": "http://127.0.0.1:8080/report"
}
```

`token_file` is optional (its contents go out as `Authorization: Bearer …`, read anew each
time, so a new token just works); `every` is the seconds between fetches (at least 3). The URL
answers with:

```json
{"items": [{"id": "1", "title": "Port the login screen", "state": "waiting",
            "detail": "May it push the branch?", "url": "https://…", "updated": 1791016000}]}
```

`state` is one of `working`, `waiting`, `idle`, `done`, `failed`, and picks the stripe colour
(waiting is yellow, failed red). What waits for you is listed first. Clicking an entry opens
its `url` on the current workspace; only http(s) links are fetched, posted to or opened. A
source that can't be reached says so under its heading and is tried again on its next turn.

**An item on its own workspace.** Right-click an entry → "Open on its own workspace": a new
kept workspace named after it, with its `url` open there as a web app window. Its tab is drawn
from the item, like a session's: its title, the stripe by its state, the state and `detail`
under it, and it turns yellow and breathes when the item starts waiting for you while you are
on another workspace. Whatever else you open there is kept and restored like on any workspace.
While it has its workspace, its tab stands for it and the group leaves it out. An item that
leaves the feed keeps its tab, drawn as a plain one; closing the workspace ends the link, and
the group lists the item again. An item may carry `"folder"`, a directory under your home: its
workspace works there ("Open terminal here" and the like).

**Every item on its own workspace.** With `"workspaces": true` a source has no list: each item
gets its workspace by itself once it is working or waiting, without taking your screen; its
page opens when you first go there. Close one and it stays closed for that item. `"only"` keeps
the items whose fields have the given values, e.g. `{"here": true}` for a feed that marks what
runs on this machine; it applies to the list and to the workspaces.

**An item in a terminal.** With `"terminal": "my-tool open {id}"`, an item's workspace opens a
terminal running that command in the item's folder instead of its page, and after a login it
comes back running it again. `{url}` is the item's link, e.g. to open its page when the command
fails: `"my-tool open {id} || sidelit open --app {url}"`. The command is yours, from your file;
the feed only supplies the id and the link, and only plain ones (an id of letters, digits, `.`,
`_`, `:`, `-`; a link without `?`, `&` or anything else a shell reads).

**Reporting back.** With `report`, the daemon sends this desktop's sessions there as a POST of
the same JSON (`{"items": [...]}`, with the same token), a couple of seconds after one changes
and every `every` seconds anyway. One item per session (several on one workspace are an item
each): `id` its session, `title` its box's title, `state` `waiting` while the box is yellow,
`working` while it is orange (busy, a shell running, waiting on CI) and `done` otherwise,
`detail` the note under the title ("waiting on CI pipeline"), `updated` when that last changed;
no `url`. Sessions on workspaces opened for an item are left out. A receiver that fails is logged once and tried again `every` seconds later; it
never holds up the daemon.
