"""The engine against a fake desktop: the situations that used to go wrong."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sidelit.core import adapters as ad  # noqa: E402
from sidelit.core import engine as eng  # noqa: E402
from sidelit.core import titles  # noqa: E402
from sidelit.core.engine import Engine  # noqa: E402
from sidelit.core.model import Ended, Layout, Workspace  # noqa: E402
from fake import Clock, FakeDesktop, run  # noqa: E402

HOME = os.path.expanduser("~")


def claude(session, title="Fix tests", cwd=HOME):
    return {"session": session, "title": title, "status": "idle", "cwd": cwd}


class Base(unittest.TestCase):
    def setUp(self):
        self.desk = FakeDesktop(spaces=3)
        self.clock = Clock()
        self.log = []
        self.layout = Layout()
        self.ctx = ad.Ctx(resume_command=lambda s, cwd: f"claude --resume {s}")

    def engine(self, startup=eng.STARTUP_S):
        self.e = Engine(self.layout, self.desk, ctx=self.ctx, journal=lambda k, **f: self.log.append((k, f)),
                        now=self.clock.t, startup_s=startup)
        return self.e

    def advance(self, seconds, step=0.5):
        run(self.e, self.desk, self.clock, seconds, step)

    def settle_calm(self):
        self.advance(eng.STARTUP_S + 2)

    def keep(self, index, name=""):
        """Register workspace `index` the way the sidebar does ('rename' / 'open terminal here')."""
        return self.e.register_space(index, self.clock.t, name=name, why="test")

    def slots(self, lw=None):
        return [s for w, s in self.layout.slots() if lw is None or w is lw]

    def kinds(self):
        return [k for k, _ in self.log]


class ClosingWindows(Base):
    def test_closed_terminal_is_forgotten_and_not_restored(self):
        """The phantom terminal: a Claude you closed must not come back at the next login."""
        w = self.desk.open("terminator", "✳ Daily upgrades", 1, claude=claude("sess-1", "Daily upgrades"))
        self.engine()
        self.settle_calm()
        home = self.layout.workspaces[0]
        self.assertEqual([s.session for s in home.slots], ["sess-1"])
        self.desk.close(w)
        self.advance(eng.DELETE_GRACE_S + 1)
        self.assertEqual(self.slots(), [])
        self.assertEqual(self.layout.recent[0]["session"], "sess-1")  # resumable from the recent list
        # next login
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(60)
        self.assertEqual(self.desk.launched, [])

    def test_logout_cascade_keeps_everything(self):
        self.desk.open("google-chrome", "Inbox - Gmail - Google Chrome", 1)
        self.desk.open("discord", "general - Discord", 1)
        t = self.desk.open("terminator", "alex@laptop: ~", 2)
        self.engine()
        self.settle_calm()
        self.keep(1)
        self.keep(2)
        self.advance(3)
        before = len(self.slots())
        self.assertEqual(before, 3)
        # the session ends: apps close one after another, then we are killed
        for wid in list(self.desk.windows):
            self.desk.crash(wid)
            self.advance(0.8)
        self.e.drop_pending()
        self.assertEqual(len(self.slots()), before)

    def test_single_close_after_a_while_is_kept_as_a_close(self):
        """A close outside a burst is a close, even if the session ends much later."""
        self.desk.open("google-chrome", "Inbox - Gmail - Google Chrome", 1)
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 1)
        self.engine()
        self.settle_calm()
        self.keep(1)
        self.advance(3)
        self.desk.close(w)
        self.advance(eng.DELETE_GRACE_S + 1)
        self.assertEqual([s.title for s in self.slots()], ["Inbox - Gmail - Google Chrome"])

    def test_app_crash_keeps_its_windows(self):
        ws = [self.desk.open("google-chrome", f"Page {i} - Google Chrome", 1) for i in range(3)]
        self.desk.open("terminator", "alex@laptop: ~", 1)
        self.engine()
        self.settle_calm()
        self.keep(1)
        self.advance(3)
        for w in ws:  # all in the same instant
            self.desk.crash(w)
        self.advance(eng.DELETE_GRACE_S + 5)
        self.assertEqual(len([s for s in self.slots() if s.app == "google-chrome"]), 3)
        self.assertTrue(any(f.get("why", "").endswith("quit or crashed") for k, f in self.log if k == "unbound"))

    def test_tray_app_keeps_its_place(self):
        d = self.desk.open("discord", "general - Discord", 1)
        self.engine()
        self.settle_calm()
        self.keep(1)
        self.keep(2)
        self.advance(3)
        self.desk.close(d)
        self.advance(eng.DELETE_GRACE_S + 2)
        self.assertEqual([s.app for s in self.slots()], ["discord"])
        # reopened from the tray while you are on workspace 2: it goes home to 1
        self.desk.active = 2
        d2 = self.desk.open("discord", "general - Discord", 2)
        self.advance(3)
        self.assertEqual(self.desk.windows[d2]["space"], 1)
        self.assertEqual(len(self.slots()), 1)


class MovingWindows(Base):
    def test_your_move_is_remembered_not_undone(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 1)
        self.engine()
        self.settle_calm()
        a, b = self.keep(1), self.keep(2)
        self.advance(3)
        self.desk.user_move(w, 2)
        self.advance(5)
        self.assertEqual(self.desk.windows[w]["space"], 2)
        self.assertEqual([s.title for s in b.slots], ["Docs - Google Chrome"])
        self.assertEqual(a.slots, [])
        self.assertEqual(self.desk.moves, [])

    def test_moving_to_the_empty_workspace_keeps_that_one_too(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 1)
        self.engine()
        self.settle_calm()
        empty = len(self.desk.spaces)  # COSMIC's empty one at the end
        self.desk.user_move(w, empty)
        self.advance(3)
        lw = self.layout.ws(self.e.owner[empty])
        self.assertTrue(self.desk.spaces[empty - 1]["pinned"])
        self.assertEqual([s.window for s in lw.slots], [w])
        self.assertEqual(self.desk.windows[w]["space"], empty)
        self.assertEqual(self.desk.moves, [])

    def test_new_window_is_kept_where_it_opened(self):
        self.engine()
        self.settle_calm()
        lw = self.keep(2)
        w = self.desk.open("google-chrome", "New Tab - Google Chrome", 2)
        self.advance(3)
        self.assertEqual([s.window for s in lw.slots], [w])
        self.assertEqual(self.desk.moves, [])

    def test_index_shift_is_not_a_move(self):
        """A workspace before ours goes away: every index after it shifts."""
        scratch = self.desk.open("gedit", "notes", 1)
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 3)
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[3])
        self.desk.active = 2
        self.desk.close(scratch)  # workspace 1 is emptied: forgotten, unpinned, gone
        self.advance(eng.DELETE_GRACE_S + eng.EMPTY_DROP_S + 3)
        self.assertEqual(self.desk.windows[w]["space"], 2)
        self.assertEqual([s.window for s in lw.slots], [w])
        self.assertEqual(self.e.primary[lw.id].index, 2)
        self.assertEqual(self.desk.moves, [])


class KeepingWorkspaces(Base):
    def test_a_workspace_you_work_on_is_kept_without_a_claude(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 3)
        self.desk.spaces[2].update(pinned=False, ids=set())  # a plain dynamic COSMIC workspace
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[3])
        self.assertTrue(self.desk.spaces[2]["pinned"])
        self.assertEqual([s.window for s in lw.slots], [w])

    def test_a_workspace_you_emptied_goes(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 2)
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[2])
        self.desk.active = 1
        self.desk.close(w)
        self.advance(eng.DELETE_GRACE_S + eng.EMPTY_DROP_S + 3)
        self.assertIsNone(self.layout.ws(lw.id))
        self.assertFalse(any(i in s["ids"] for s in self.desk.spaces for i in lw.ids))

    def test_a_workspace_whose_windows_crashed_stays(self):
        for i in range(2):
            self.desk.open("google-chrome", f"Page {i} - Google Chrome", 2)
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[2])
        for wid in list(self.desk.windows):
            self.desk.crash(wid)  # Chrome crashed
        self.advance(eng.DELETE_GRACE_S + eng.EMPTY_DROP_S + 3)
        self.assertIs(self.layout.ws(lw.id), lw)
        self.assertEqual(len(lw.slots), 2)

    def test_a_named_workspace_stays_empty(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 2)
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[2])
        self.e.rename(lw.id, "Research")
        self.desk.close(w)
        self.advance(eng.DELETE_GRACE_S + eng.EMPTY_DROP_S + 3)
        self.assertIs(self.layout.ws(lw.id), lw)


class Screens(Base):
    def docked(self):
        """Terminals on the monitor, browsers on the laptop, a workspace each (0.4's desk)."""
        self.t1 = self.desk.open("terminator", "✳ Deploy", 1, output="HDMI-A-1", claude=claude("sess-1", "Deploy"))
        self.c1 = self.desk.open("google-chrome", "Docs - Google Chrome", 1, output="eDP-1")
        self.t2 = self.desk.open("terminator", "✳ Pricing", 2, output="HDMI-A-1", claude=claude("sess-2", "Pricing"))
        self.c2 = self.desk.open("google-chrome", "Sheets - Google Chrome", 2, output="eDP-1")
        self.engine()
        self.settle_calm()
        self.advance(eng.ALIAS_SETTLE_S + 1)  # every copy pinned, and its id learnt
        self.a, self.b =(self.layout.ws(self.e.owner[i]) for i in (1, 2))
        self.home = {w: (d["space"], d["output"]) for w, d in self.desk.windows.items()}

    def where(self):
        return {w: (d["space"], d["output"]) for w, d in self.desk.windows.items()}

    def test_every_screens_copy_is_pinned_and_known(self):
        self.docked()
        for lw, i in ((self.a, 1), (self.b, 2)):
            self.assertTrue(self.desk.spaces[i - 1].all_pinned)
            self.assertEqual(set(lw.ids), set(self.desk.spaces[i - 1].ids))
            self.assertEqual(len(lw.ids), 2)
        self.assertEqual(self.desk.moves, [])

    def test_lid_shut_keeps_the_monitors_windows_where_they_are(self):
        """The laptop screen goes (lid shut on the dock): its workspaces are appended to the
        monitor. The monitor's windows stay put; the laptop's join them on their own
        workspace; opening the lid sends them back."""
        self.docked()
        self.desk.unplug("eDP-1")
        self.advance(eng.OUTPUT_SETTLE_S + 4)
        self.assertEqual(self.where()[self.t1], (1, "HDMI-A-1"))
        self.assertEqual(self.where()[self.t2], (2, "HDMI-A-1"))
        self.assertEqual(self.where()[self.c1], (1, "HDMI-A-1"))
        self.assertEqual(self.where()[self.c2], (2, "HDMI-A-1"))
        self.assertFalse(any(m[0] in (self.t1, self.t2) for m in self.desk.moves))
        self.advance(eng.OUTPUTS_S)
        self.assertEqual(len(self.desk.spaces), 3)  # the appended copies are gone again
        self.assertEqual([s.output for s in self.a.slots], ["HDMI-A-1", "eDP-1"])  # still remembered
        self.desk.replug("eDP-1")
        self.advance(eng.OUTPUTS_S + 5)
        self.assertEqual(self.where(), self.home)
        self.assertEqual(len(self.slots()), 4)
        self.assertTrue(all(s.all_pinned for s in self.desk.spaces[:2]))

    def test_undocking_and_redocking(self):
        self.docked()
        count = len(self.desk.spaces)
        self.desk.unplug("HDMI-A-1")
        self.advance(eng.OUTPUT_SETTLE_S + 4)
        self.assertEqual(self.where()[self.t1], (1, "eDP-1"))
        self.assertEqual(self.where()[self.t2], (2, "eDP-1"))
        self.advance(600)  # an afternoon on the laptop
        self.assertEqual(len(self.desk.spaces), count)  # the appended copies are gone again
        self.desk.replug("HDMI-A-1")
        self.advance(eng.OUTPUTS_S + 5)
        self.assertEqual(self.where(), self.home)
        self.assertEqual(len(self.slots()), 4)
        self.assertEqual(len(self.layout.workspaces), 2)

    def test_a_screen_gone_for_a_moment_moves_nothing(self):
        """The monitor dozing off behind the lock screen, a wobbly cable: cosmic puts
        everything back itself, so nothing is moved at all."""
        self.docked()
        for _ in range(3):
            self.desk.unplug("HDMI-A-1")
            self.advance(3)
            self.desk.replug("HDMI-A-1")
            self.advance(2)
        self.advance(eng.OUTPUTS_S + 5)
        self.assertEqual(self.desk.moves, [])
        self.assertEqual(self.where(), self.home)
        self.assertEqual(len(self.slots()), 4)

    def test_both_ways_while_asleep(self):
        """Suspended docked, woken on the laptop alone, then the dock: everything ends up home."""
        self.docked()
        self.e.suspend_resumed(self.clock.t)
        self.desk.unplug("HDMI-A-1")
        self.advance(20)
        self.desk.replug("HDMI-A-1")
        self.desk.unplug("eDP-1")
        self.advance(20)
        self.desk.replug("eDP-1")
        self.advance(eng.OUTPUTS_S + 5)
        self.assertEqual(self.where(), self.home)
        self.assertEqual(len(self.slots()), 4)

    def test_a_layout_that_knows_one_copy_learns_the_other(self):
        """0.4.0 layouts carry the laptop copy's id only: after an upgrade the monitor's copy
        is pinned and learnt too."""
        self.desk.open("terminator", "✳ Deploy", 1, output="HDMI-A-1", claude=claude("sess-1", "Deploy"))
        self.engine()
        self.settle_calm()
        lw = self.layout.workspaces[0]
        self.advance(eng.ALIAS_SETTLE_S + 1)
        self.assertEqual(len(lw.ids), 2)
        self.assertIn("pinned_copies", self.kinds())

    def test_moving_a_window_to_another_screen_is_remembered(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 1, output="eDP-1")
        self.engine()
        self.settle_calm()
        lw = self.keep(1)
        self.advance(3)
        self.desk.user_move(w, 1, output="HDMI-A-1")
        self.advance(3)
        self.assertEqual(lw.slots[0].output, "HDMI-A-1")
        self.assertEqual(self.desk.moves, [])

    def test_a_window_the_compositor_will_not_move_is_left_alone(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 1)
        self.engine()
        self.settle_calm()
        self.keep(1)
        self.keep(2)
        self.advance(3)
        self.desk.refuse_moves = True
        self.e.suspend_resumed(self.clock.t)
        self.desk.windows[w]["space"] = 2  # shoved away on wake, and it will not budge
        self.advance(20)
        self.assertLessEqual(len(self.desk.moves), eng.MOVE_TRIES)
        self.assertIn("gave_up", self.kinds())


class OutOfStep(Base):
    """COSMIC keeps a list of workspaces per screen. A close or a replug reaches each list on its
    own, and a replug slots the returning screen's copies back by position: for a while, or for
    good, one index can hold copies of two different workspaces (2026-10-01)."""

    def setUp(self):
        super().setUp()
        self.desk = FakeDesktop(spaces=4)

    def four(self):
        """A Claude on the monitor on each of four workspaces, every copy pinned and known."""
        self.t = [self.desk.open("terminator", f"✳ Task {i}", i, output="HDMI-A-1",
                                 claude=claude(f"sess-{i}", f"Task {i}")) for i in (1, 2, 3, 4)]
        self.engine()
        self.settle_calm()
        self.advance(eng.ALIAS_SETTLE_S + 1)
        self.lws = [self.layout.ws(self.e.owner[i]) for i in (1, 2, 3, 4)]
        self.log.clear()

    def windows_of(self, lw):
        return [s.window for s in lw.slots]

    def test_closing_a_workspace_while_one_screen_hears_first_moves_nothing(self):
        """You close workspace 2 while on it: the laptop's list drops it at once, the monitor's
        only once its windows are gone. Meanwhile workspace 3's monitor copy sits at index 3 next
        to workspace 4's laptop copy. Nobody moved anything: nothing is recorded as moved, the
        last workspace's Claude is not forgotten, and no id is learnt from the mix-up."""
        self.four()
        a, b, c, d = self.lws
        self.desk.active = 2
        self.e.leave_when_empty(2, self.clock.t)
        wins = self.e.remove_workspace(b.id, self.clock.t, "closed by you")
        self.desk.drop_copy(2, "eDP-1")
        self.advance(1)
        for w in wins:
            self.desk.close(w)
        self.desk.drop_copy(2, "HDMI-A-1")
        self.advance(eng.MIXED_HOLD_S + eng.ALIAS_SETTLE_S + 5)
        self.assertNotIn("moved", self.kinds())
        self.assertNotIn("slot_deleted", self.kinds())
        self.assertNotIn("slot_created", self.kinds())
        self.assertEqual([self.windows_of(lw) for lw in (a, c, d)], [[self.t[0]], [self.t[2]], [self.t[3]]])
        self.assertEqual(self.desk.moves, [])
        for lw in (a, c, d):
            self.assertEqual(set(lw.ids), self.desk.spaces[self.e.primary[lw.id].index - 1].ids)

    def test_a_window_left_on_a_copy_slotted_back_elsewhere_stays_with_its_workspace(self):
        """A replug puts workspace 2's monitor copy back at index 3, next to workspace 3's laptop
        copy (and 3's at 2). A Claude that was on 2's copy all along is 2's: it is not taken
        into 3 because that is the index it now shows at."""
        self.four()
        a, b, c, d = self.lws
        stray = self.desk.open("terminator", "✳ Config references", 2, output="HDMI-A-1",
                               claude=claude("sess-x", "Config references"))
        self.e.forget_slot(self.e.bind[stray], self.clock.t) if stray in self.e.bind else None
        self.e.bind.pop(stray, None)
        self.e.first_seen[stray] = self.clock.t - 600  # there long before, not kept (it was dropped)
        self.e._initial.add(stray)
        two, three = self.desk.spaces[1].copies, self.desk.spaces[2].copies
        two["HDMI-A-1"], three["HDMI-A-1"] = three["HDMI-A-1"], two["HDMI-A-1"]
        for w in self.desk.windows.values():
            if w["output"] == "HDMI-A-1" and w["space"] in (2, 3):
                w["space"] = 5 - w["space"]
        self.advance(eng.MIXED_HOLD_S + eng.ALIAS_SETTLE_S + 5)
        stray_slot = next(s for _, s in self.layout.slots() if s.window == stray)
        self.assertIs(self.layout.find(stray_slot.id)[0], b)
        index = {lw.id: self.e.primary[lw.id].index for lw in self.lws}
        for lw, wins in ((b, [self.t[1], stray]), (c, [self.t[2]])):
            self.assertEqual(sorted(self.windows_of(lw)), sorted(wins))
            self.assertTrue(all(self.desk.windows[w]["space"] == index[lw.id] for w in wins))
        self.assertNotIn("moved", self.kinds())

    def test_ids_another_workspace_lives_on_are_let_go(self):
        """A layout that learnt a neighbour's id in an earlier mix-up gives it back."""
        self.four()
        a, b, c, d = self.lws
        stolen = self.desk.spaces[0].copies["HDMI-A-1"]["id"]
        c.ids.append(stolen)
        self.advance(eng.MIXED_HOLD_S + 2)
        self.assertNotIn(stolen, c.ids)
        self.assertIn(stolen, a.ids)
        self.assertIn("ids_dropped", self.kinds())
        self.assertEqual(self.desk.moves, [])

    def test_after_a_replug_the_order_you_set_is_put_back(self):
        """COSMIC came back from an unplug with workspace 3 first: it is put back where you had it,
        and the layout keeps your order instead of taking COSMIC's."""
        self.four()
        order = [lw.id for lw in self.layout.workspaces]
        home = {w: (d["space"], d["output"]) for w, d in self.desk.windows.items()}
        self.desk.unplug("HDMI-A-1")
        self.advance(eng.OUTPUT_SETTLE_S + 4)
        self.desk.replug("HDMI-A-1")
        self.desk.reshuffle(3, 1)
        self.advance(eng.OUTPUTS_S + 20)
        self.assertEqual([lw.id for lw in self.layout.workspaces], order)
        self.assertEqual([self.e.primary[i].index for i in order], [1, 2, 3, 4])
        self.assertEqual({w: (d["space"], d["output"]) for w, d in self.desk.windows.items()}, home)
        self.assertIn("reordered", self.kinds())

    def test_a_desktop_that_cannot_reorder_still_gets_its_windows_back(self):
        """A desktop with no reorder: one try, then windows go home as before, without waiting on it."""
        self.four()
        self.desk.move_space = lambda index, before: False
        self.desk.unplug("HDMI-A-1")
        self.advance(eng.OUTPUT_SETTLE_S + 4)
        self.desk.replug("HDMI-A-1")
        self.desk.reshuffle(3, 1)
        self.advance(eng.OUTPUT_SETTLE_S + 4)
        self.assertEqual(self.kinds().count("reordered"), 1)
        for lw, t in zip(self.lws, self.t):
            self.assertEqual(self.desk.windows[t]["space"], self.e.primary[lw.id].index)

    def test_reordering_workspaces_yourself_is_kept(self):
        """Long after any screen change, an order you set in COSMIC's overview is yours."""
        self.four()
        a, b, c, d = self.lws
        self.desk.reshuffle(3, 1)
        self.advance(5)
        self.assertEqual(self.layout.workspaces[:4], [c, a, b, d])
        self.assertEqual(self.desk.moved_spaces, [])


class Restarts(Base):
    def test_daemon_restart_rebinds_by_window_identifier(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 2)
        self.engine()
        self.settle_calm()
        lw = self.keep(2)
        self.advance(3)
        # restart: a new engine on the saved layout
        self.log.clear()
        self.engine()
        self.advance(1)
        self.assertEqual(self.e.bind.get(w), lw.slots[0].id)
        self.settle_calm()
        self.assertEqual(len(self.slots()), 1)

    def test_scratch_window_is_not_pulled_in_after_a_restart(self):
        """An unkept window that happens to share a title with a missing slot stays put."""
        self.engine()
        self.settle_calm()
        lw = self.keep(1)
        gone = self.desk.open("google-chrome", "Inbox - Gmail - Google Chrome", 1)
        self.advance(3)
        self.desk.close(gone)
        self.e.drop_pending()  # we died before the close stood: the slot is missing, not deleted
        scratch = self.desk.open("google-chrome", "Inbox - Gmail - Google Chrome", 3)
        self.engine()
        self.advance(eng.STARTUP_S + 4)
        self.assertEqual(self.desk.windows[scratch]["space"], 3)
        self.assertEqual(self.desk.moves, [])
        self.assertEqual(len(lw.slots), 1)


class Migration(Base):
    def test_absorb_waits_for_the_compositor_to_say_where_windows_are(self):
        """Right after connecting, COSMIC has not yet said which workspace a window is on."""
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 2)
        self.engine()
        self.settle_calm()
        a, b = self.keep(1), self.keep(2)
        self.advance(3)
        # a stale snapshot: the migrated layout has the window on workspace 1
        slot = b.slots.pop()
        a.slots.append(slot)
        self.desk.moves.clear()
        self.engine()
        self.e.absorb_until = self.clock.t + 15
        real = self.desk.obs

        def blind():  # the first observations: no workspace for any window yet
            o = real()
            o.windows = {k: v.__class__(**{**v.__dict__, "space": None}) for k, v in o.windows.items()}
            return o
        self.desk.obs = blind
        self.advance(1)
        self.desk.obs = real
        self.advance(eng.STARTUP_S + 5)
        self.assertEqual(self.desk.moves, [])
        self.assertEqual(b.slots, [slot])


class Login(Base):
    def _layout_from_last_session(self):
        """Yesterday: a Claude on 1, Chrome windows on 1 and 2, Discord in Chrome on 2."""
        self.desk.open("terminator", "✳ Deploy", 1, claude=claude("sess-9", "Deploy"))
        self.desk.open("google-chrome", "Pipelines · GitLab - Google Chrome", 1)
        self.desk.open("google-chrome", "Calendar - Week of 22 - Google Chrome", 2)
        self.desk.open("chrome-discord.com__app-Default", "Discord | #general", 2)
        self.engine()
        self.settle_calm()
        self.keep(2)
        self.advance(3)
        self.assertEqual(len(self.slots()), 4)
        # reboot: the compositor keeps pinned workspaces, the windows are gone
        for w in list(self.desk.windows):
            self.desk.windows.pop(w)
        self.desk.claude.clear()
        self.e.drop_pending()
        self.desk.moves.clear()

    def test_login_restore_resumes_claude_and_places_chrome(self):
        self._layout_from_last_session()
        self.engine()
        self.e.start_restore(self.clock.t, "login", delay=2)
        self.advance(3)
        # the Claude terminal is started first, on its workspace
        self.assertEqual(self.desk.launched[0]["type"], "terminal")
        self.assertIn("sess-9", self.desk.launched[0]["command"])
        self.desk.open("terminator", "✳ Deploy", self.desk.active, claude=claude("sess-9", "Deploy"))
        self.advance(8)
        apps = [s.get("app") for s in self.desk.launched[1:]]
        self.assertEqual(apps, ["google-chrome"])  # once: it restores its own windows
        # Chrome brings its windows back wherever it likes, one with a different title
        c1 = self.desk.open("google-chrome", "Pipelines · GitLab - Google Chrome", 1)
        c2 = self.desk.open("google-chrome", "Calendar - Week of 23 - Google Chrome", 1)
        self.advance(30)
        # the web app was not restored by the browser: we open it
        self.assertTrue(any(s["type"] == "webapp" for s in self.desk.launched))
        self.desk.open("chrome-discord.com__app-Default", "Discord | #general", 1)
        self.advance(eng.RESTORE_APP_GRACE_S + 10)
        self.assertTrue(self.e.restore.finished)
        self.assertEqual(self.desk.windows[c1]["space"], 1)
        self.assertEqual(self.desk.windows[c2]["space"], 2)
        webapp = next(w for w, d in self.desk.windows.items() if d["app"].startswith("chrome-discord"))
        self.assertEqual(self.desk.windows[webapp]["space"], 2)
        self.assertEqual(len(self.slots()), 4)
        self.assertEqual(len(self.e.bind), 4)

    def test_what_is_missing_mid_session_is_started_again_quietly(self):
        """The layout is what the desktop should be: missing windows come back without a login,
        and without the workspaces flicking past."""
        self._layout_from_last_session()
        self.engine()
        self.advance(eng.STARTUP_S + eng.RELAUNCH_AFTER_S + 60)
        self.assertTrue(self.desk.launched)
        self.assertEqual(self.desk.activations, [])
        self.assertIn("relaunch", self.kinds())

    def test_relaunching_can_be_turned_off(self):
        self._layout_from_last_session()
        self.engine()
        self.e.relaunch = False
        self.advance(120)
        self.assertEqual(self.desk.launched, [])
        self.assertEqual(len(self.slots()), 4)  # missing, not forgotten

    def test_slots_that_never_come_back_expire(self):
        self.desk.open("gedit", "notes.txt - gedit", 1)
        self.engine()
        self.settle_calm()
        self.keep(1)
        self.advance(3)
        self.desk.windows.clear()
        self.e.drop_pending()
        for _ in range(eng.MAX_MISSED):
            self.engine()
            self.e.start_restore(self.clock.t, "login", delay=1)
            # gedit was started but never showed a window: the restore waits for it up to the cap
            self.advance(eng.RESTORE_MAX_S + 5, step=2)
            self.assertTrue(self.e.restore.finished)
        self.assertEqual(self.slots(), [])


class Claudes(Base):
    def test_a_claude_makes_its_workspace_kept(self):
        self.engine()
        self.settle_calm()
        self.desk.open("terminator", "✳ Fix tests", 3, claude=claude("sess-3"))
        self.advance(3)
        lw = self.layout.ws(self.e.owner[3])
        self.assertTrue(self.desk.spaces[2]["pinned"])
        self.assertEqual(lw.claude.session, "sess-3")

    def test_claude_exiting_leaves_a_plain_terminal(self):
        w = self.desk.open("terminator", "✳ Fix tests", 1, claude=claude("sess-4"))
        self.engine()
        self.settle_calm()
        self.desk.claude.pop(w)
        self.desk.windows[w]["title"] = f"alex@laptop: {HOME}"
        self.advance(adapters_gone := ad.CLAUDE_GONE_S + 3)
        slot = self.slots()[0]
        self.assertIsNone(slot.session)
        self.assertEqual(slot.state.get("cwd"), HOME)
        self.assertEqual(self.layout.recent[0]["session"], "sess-4")
        self.assertGreater(adapters_gone, 0)

    def test_resuming_elsewhere_supersedes_the_missing_slot(self):
        w = self.desk.open("terminator", "✳ Fix tests", 1, claude=claude("sess-5"))
        self.engine()
        self.settle_calm()
        self.desk.close(w)
        self.e.drop_pending()  # (a restart: the slot is missing)
        self.engine()
        self.settle_calm()
        self.keep(2)
        self.desk.open("terminator", "✳ Fix tests", 2, claude=claude("sess-5"))
        self.advance(4)
        sessions = [s.session for s in self.slots()]
        self.assertEqual(sessions, ["sess-5"])


def panes(*claudes):
    """A terminal window showing several Claudes (split panes, tabs), as the daemon reports it."""
    return {**claudes[0], "more": list(claudes[1:])}


class SeveralClaudes(Base):
    """Two or three Claudes on one workspace: in windows of their own, or sharing one."""

    def window(self, *claudes, space=1):
        return self.desk.open("terminator", "✳ " + claudes[0]["title"], space, claude=panes(*claudes))

    def sessions(self):
        return [[c["session"] for c in s.claudes] for s in self.slots()]

    def test_a_window_remembers_every_claude_in_it(self):
        self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"))
        self.engine()
        self.settle_calm()
        self.assertEqual(self.sessions(), [["s1", "s2"]])
        self.assertEqual(ad.adapter_of(self.slots()[0]).describe(self.slots()[0]), "Claude 'Pricing API' and 1 more")

    def test_closing_the_window_lists_each_in_recent_sessions(self):
        w = self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"))
        self.engine()
        self.settle_calm()
        self.desk.close(w)
        self.advance(eng.DELETE_GRACE_S + 1)
        self.assertEqual([(r["session"], r["title"]) for r in self.layout.recent],
                         [("s1", "Pricing API"), ("s2", "Review tests")])

    def test_one_that_ends_while_the_window_stays_goes_to_recent_sessions(self):
        w = self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"), claude("s3", "Ask about CSV"))
        self.engine()
        self.settle_calm()
        self.desk.claude[w] = panes(claude("s1", "Pricing API"), claude("s3", "Ask about CSV"))
        self.advance(2)
        self.assertEqual(self.sessions(), [["s1", "s3"]])
        self.assertEqual([r["session"] for r in self.layout.recent], ["s2"])
        # the first one ending leaves the other as the window's Claude
        self.desk.claude[w] = claude("s3", "Ask about CSV")
        self.advance(2)
        self.assertEqual(self.sessions(), [["s3"]])
        self.assertEqual([r["session"] for r in self.layout.recent], ["s1", "s2"])

    def test_clear_in_one_pane_is_not_an_end(self):
        w = self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"))
        self.engine()
        self.settle_calm()
        self.desk.claude[w] = panes(claude("s1", "Pricing API"), claude("s2b", "New"))
        self.advance(2)
        self.assertEqual((self.sessions(), self.layout.recent), ([["s1", "s2b"]], []))

    def test_login_restore_brings_each_back_in_a_terminal_of_its_own(self):
        """Three Claudes on workspace 1: two sharing a window, one in a window of its own."""
        self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"))
        self.window(claude("s3", "Ask about CSV"))
        self.engine()
        self.settle_calm()
        for w in list(self.desk.windows):  # reboot
            self.desk.windows.pop(w)
        self.desk.claude.clear()
        self.e.drop_pending()
        self.engine()
        self.desk.spawn = True
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(60)
        resumed = sorted(spec["command"] for spec in self.desk.launched)
        self.assertEqual(resumed, ["claude --resume s1", "claude --resume s2", "claude --resume s3"])
        self.assertEqual(sorted(s for ss in self.sessions() for s in ss), ["s1", "s2", "s3"])
        self.assertEqual(len(self.e.bind), 3)  # each in its own window, all on workspace 1
        self.assertTrue(all(d["space"] == 1 for d in self.desk.windows.values()))
        self.assertEqual(len({lw.id for lw, _ in self.layout.slots()}), 1)

    def test_a_closed_workspace_brings_each_back_from_the_history(self):
        self.window(claude("s1", "Pricing API", "/tmp"), claude("s2", "Review tests", "/tmp"), space=2)
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[2])
        for wid in self.e.remove_workspace(lw.id, self.clock.t, "closed by you"):
            self.desk.close(wid)
        self.advance(3)
        snap = self.layout.history[0]["workspace"]["slots"][0]
        self.assertEqual([m["session"] for m in snap["state"]["more"]], ["s2"])
        self.assertEqual([r["session"] for r in self.layout.recent], ["s1", "s2"])
        self.desk.spawn = True
        self.e.restore_from_history(self.layout.history[0]["id"], self.clock.t)
        self.advance(40)
        self.assertEqual(sorted(spec["command"] for spec in self.desk.launched),
                         ["claude --resume s1", "claude --resume s2"])
        self.assertEqual(self.layout.recent, [])

    def test_one_open_somewhere_already_is_not_resumed_twice(self):
        self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"))
        self.engine()
        self.settle_calm()
        for w in list(self.desk.windows):
            self.desk.windows.pop(w)
        self.desk.claude.clear()
        self.e.drop_pending()
        self.engine()
        self.desk.open("terminator", "✳ Review tests", 2, claude=claude("s2", "Review tests"))  # you resumed it
        self.keep(2)
        self.advance(3)
        self.desk.spawn = True
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(60)
        self.assertEqual([spec["command"] for spec in self.desk.launched], ["claude --resume s1"])

    def test_resuming_the_first_elsewhere_keeps_the_others_it_shared_a_window_with(self):
        self.window(claude("s1", "Pricing API"), claude("s2", "Review tests"), claude("s3", "Ask about CSV"))
        self.engine()
        self.settle_calm()
        for w in list(self.desk.windows):
            self.desk.windows.pop(w)
        self.desk.claude.clear()
        self.e.drop_pending()
        self.engine()
        self.desk.open("terminator", "✳ Pricing API", 2, claude=claude("s1", "Pricing API"))  # you resumed it
        self.settle_calm()
        self.assertEqual(sorted(s for ss in self.sessions() for s in ss), ["s1", "s2", "s3"])
        self.assertEqual(self.layout.recent, [])  # (they did not end: they were not open)
        self.desk.spawn = True
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(60)
        self.assertEqual(sorted(spec["command"] for spec in self.desk.launched),
                         ["claude --resume s2", "claude --resume s3"])


class Commands(Base):
    def test_moving_a_slot_in_the_layout_moves_the_window_and_is_not_undone(self):
        w = self.desk.open("google-chrome", "Docs - Google Chrome", 1)
        self.engine()
        self.settle_calm()
        a, b = self.keep(1), self.keep(2)
        self.advance(3)
        slot = a.slots.pop()
        b.slots.append(slot)  # what a "move to workspace" command does
        self.advance(3)
        self.assertEqual(self.desk.windows[w]["space"], 2)
        self.assertEqual(b.slots, [slot])
        self.advance(10)
        self.assertEqual(len(self.desk.moves), 1)

    def test_remove_workspace_does_not_readopt_it(self):
        self.desk.open("terminator", "✳ Fix tests", 2, claude=claude("sess-6"))
        self.engine()
        self.settle_calm()
        lw = self.layout.ws(self.e.owner[2])
        wins = self.e.remove_workspace(lw.id, self.clock.t, "closed by you")
        self.assertEqual(len(wins), 1)
        self.advance(5)  # the terminal is still closing
        self.assertEqual(self.layout.workspaces, [])
        self.assertEqual(self.layout.recent[0]["session"], "sess-6")


class ClosingWorkspaces(Base):
    """Close in the sidebar on the workspace you are on: once its windows are gone you are taken
    to the next one, so COSMIC drops the empty one instead of keeping it until you switch."""

    def three(self, on=2):
        wins = [self.desk.open("terminator", f"alex@laptop: ~/{i}", i) for i in (1, 2, 3)]
        self.desk.active = on
        self.engine()
        self.settle_calm()
        return wins

    def close_workspace(self, index, close_windows=True):
        """What the sidebar's Close does (the daemon's cmd_close)."""
        self.e.leave_when_empty(index, self.clock.t)
        wins = self.e.remove_workspace(self.e.owner[index], self.clock.t, "closed by you")
        if close_windows:
            for w in wins:
                self.desk.close(w)
        return wins

    def test_you_are_taken_to_the_workspace_below(self):
        wins = self.three()
        self.close_workspace(2)
        self.advance(1)
        self.assertEqual(self.desk.activations, [3])
        self.assertEqual(self.desk.windows[wins[2]]["space"], self.desk.active)  # the empty one went
        self.assertEqual(len(self.desk.spaces), 3)  # 1, the old 3, the trailing empty one

    def test_closing_the_last_one_lands_on_the_empty_one_below(self):
        """COSMIC always keeps an empty workspace at the end: ready to start something new."""
        wins = self.three(on=3)
        self.close_workspace(3)
        self.advance(1)
        self.assertEqual(self.desk.activations, [4])
        self.assertEqual(len(self.desk.spaces), 3)  # 1, 2 and the empty one you are on
        self.assertEqual(self.desk.active, 3)
        self.assertFalse(any(w["space"] == 3 for w in self.desk.windows.values()))
        self.assertEqual(self.desk.windows[wins[1]]["space"], 2)

    def test_a_window_that_will_not_close_keeps_you_there(self):
        """An app asking about unsaved work: you stay to answer it."""
        self.three()
        self.close_workspace(2, close_windows=False)
        self.advance(eng.LEAVE_WAIT_S + 2)
        self.assertEqual(self.desk.activations, [])
        self.assertEqual(self.desk.active, 2)

    def test_closing_another_workspace_leaves_you_where_you_are(self):
        self.three(on=1)
        self.close_workspace(2)
        self.advance(3)
        self.assertEqual(self.desk.activations, [])
        self.assertEqual(self.desk.active, 1)


class ItemWorkspaces(Base):
    """An other source's item on a workspace of its own (the daemon's item_workspace)."""
    APP = "chrome-jobs.example.test__job_j1-Default"  # a browser app window for https://jobs.example.test/job/j1

    def open_item(self):
        self.engine()
        self.settle_calm()
        lw = self.e.create_workspace(self.clock.t, name="Port the login screen", cwd=HOME, source="jobs", item="j1")
        index = self.e.primary[lw.id].index
        self.e.expect_window(lw.id, self.clock.t, lambda w: titles.chrome_app_url(w.app) is not None, what="web app")
        self.desk.open(self.APP, "Port the login screen", index)
        self.advance(3)
        return lw, index

    def test_the_workspace_is_kept_bound_to_its_item(self):
        lw, index = self.open_item()
        self.assertIs(self.layout.for_item("jobs", "j1"), lw)
        self.assertIsNone(self.layout.for_item("jobs", "j2"))
        self.assertEqual([s.kind for s in lw.slots], ["webapp"])
        self.assertEqual(lw.slots[0].state["url"], "https://jobs.example.test/job/j1")
        saved = Layout.from_json(self.layout.to_json()).ws(lw.id)
        self.assertEqual((saved.source, saved.item, saved.name), ("jobs", "j1", "Port the login screen"))

    def test_what_else_opens_there_is_kept_and_it_all_comes_back(self):
        lw, index = self.open_item()
        self.desk.open("terminator", "alex@laptop: ~", index)
        self.advance(3)
        self.assertEqual(sorted(s.kind for s in lw.slots), ["terminal", "webapp"])
        for w in list(self.desk.windows):  # the next login
            self.desk.windows.pop(w)
        self.e.drop_pending()
        self.engine()
        self.e.start_restore(self.clock.t, "login", delay=1)
        self.advance(eng.RESTORE_APP_GRACE_S + 30)
        webapp = next(s for s in self.desk.launched if s["type"] == "webapp")
        self.assertEqual(webapp["url"], "https://jobs.example.test/job/j1")
        self.assertEqual(self.layout.for_item("jobs", "j1"), lw)

    def test_a_terminal_opened_for_it_comes_back_with_its_own_command(self):
        lw, index = self.open_item()
        cmd = "zsh -i -c 'my-tool open j1; exec zsh -i'"
        self.e.expect_window(lw.id, self.clock.t, lambda w: w.app == "terminator", what="terminal",
                             state={"command": cmd})
        self.desk.open("terminator", "my-tool", index)
        self.advance(3)
        term = next(s for s in lw.slots if s.kind == "terminal")
        self.assertEqual(term.state["command"], cmd)
        term.state["session"] = "sess-7"  # the Claude it runs shows up in it
        spec = ad.adapter_of(term).launch(term, self.ctx)
        self.assertEqual((spec["type"], spec["command"]), ("terminal", cmd))  # not a bare `claude --resume`

    def test_emptied_it_stays(self):
        """It is named after its item: closing its windows one by one does not drop it."""
        lw, index = self.open_item()
        for w in list(self.desk.windows):
            self.desk.close(w)
        self.advance(eng.DELETE_GRACE_S + eng.EMPTY_DROP_S + 5)
        self.assertIs(self.layout.for_item("jobs", "j1"), lw)

    def test_closing_it_ends_the_binding(self):
        lw, index = self.open_item()
        self.e.remove_workspace(lw.id, self.clock.t, "closed by you")
        self.assertIsNone(self.layout.for_item("jobs", "j1"))
        self.assertTrue(self.layout.had_item("jobs", "j1"))  # so it does not open again by itself
        self.assertTrue(Layout.from_json(self.layout.to_json()).had_item("jobs", "j1"))
        back = self.e.restore_from_history(self.layout.history[0]["id"], self.clock.t)
        self.assertEqual((back.name, back.source, back.item), ("Port the login screen", "", ""))

    def test_an_old_layout_has_no_items(self):
        self.assertEqual(Workspace.from_json({"id": "w1"}).source, "")
        self.assertNotIn("source", Workspace(id="w1").to_json())
        self.assertNotIn("opened_items", Layout().to_json())
        self.assertEqual(Layout.from_json({"workspaces": [], "opened_items": [["a", "1"], "junk", [1]]}).opened_items,
                         [["a", "1"]])

    def test_the_items_it_remembers_are_capped(self):
        lay = Layout()
        for i in range(Layout.OPENED_ITEMS_MAX + 5):
            lay.remember_item("jobs", str(i))
        self.assertEqual(len(lay.opened_items), Layout.OPENED_ITEMS_MAX)
        self.assertFalse(lay.had_item("jobs", "0"))
        self.assertTrue(lay.had_item("jobs", str(Layout.OPENED_ITEMS_MAX + 4)))


class Relaunching(Base):
    def claudes(self):
        self.desk.spawn = True
        wins = [self.desk.open("terminator", f"✳ Task {i}", i, claude=claude(f"sess-{i}", f"Task {i}", "/tmp"))
                for i in (1, 2)]
        self.engine()
        self.settle_calm()
        self.advance(3)
        return wins

    def test_a_crashed_app_comes_back(self):
        """Every terminal went at once (Terminator or XWayland crashed): both Claudes are resumed
        on their own workspaces, without anyone asking."""
        wins = self.claudes()
        slots = {w: self.e.bind[w] for w in wins}
        for w in wins:
            self.desk.crash(w)
        self.advance(eng.RELAUNCH_AFTER_S + 40)
        commands = sorted(l["command"] for l in self.desk.launched)
        self.assertEqual(commands, ["claude --resume sess-1", "claude --resume sess-2"])
        self.assertEqual(sorted(self.e.bind.values()), sorted(slots.values()))
        for wid, sid in self.e.bind.items():
            lw, _ = self.layout.find(sid)
            self.assertEqual(self.desk.windows[wid]["space"], self.e.primary[lw.id].index)
        self.assertEqual(self.desk.activations, [])

    def test_one_terminal_crashing_on_its_own_comes_back(self):
        """Each terminal is its own process: one dying while you work elsewhere is a crash."""
        wins = self.claudes()
        self.desk.crash(wins[1])
        self.advance(eng.RELAUNCH_AFTER_S + 20)
        self.assertEqual([l["command"] for l in self.desk.launched], ["claude --resume sess-2"])
        self.assertEqual(len(self.slots()), 2)

    def test_clicking_the_close_button_of_another_window_is_you_closing_it(self):
        """The click focuses it a moment before it goes, faster than we look: the compositor's
        focus event still says so."""
        wins = self.claudes()
        self.desk.focused_at[wins[1]] = self.clock.t + 0.1
        self.desk.crash(wins[1])  # (our last look saw it unfocused)
        self.advance(eng.DELETE_GRACE_S + 20)
        self.assertEqual(self.desk.launched, [])
        self.assertEqual(len(self.slots()), 1)

    def test_a_window_you_closed_is_not_started_again(self):
        wins = self.claudes()
        self.desk.close(wins[0])
        self.advance(eng.DELETE_GRACE_S + 30)
        self.assertEqual(self.desk.launched, [])
        self.assertEqual(len(self.slots()), 1)

    def test_a_tray_app_you_closed_stays_closed(self):
        self.desk.open("discord", "general - Discord", 1)
        self.engine()
        self.settle_calm()
        self.advance(3)
        self.desk.close(next(iter(self.desk.windows)))
        self.advance(60)
        self.assertEqual(self.desk.launched, [])
        self.assertEqual(len(self.slots()), 1)  # kept: it comes back when you open it

    def test_something_that_keeps_dying_is_given_up_on(self):
        wins = self.claudes()
        for w in wins:
            self.desk.crash(w)
        for _ in range(40):  # every relaunched window dies again at once, past the whole backoff
            self.advance(10)
            for w in list(self.desk.windows):
                self.desk.crash(w)
        self.advance(60)
        self.assertLessEqual(len(self.desk.launched), 2 * len(eng.RELAUNCH_BACKOFF_S))
        self.assertIn("relaunch_gave_up", self.kinds())
        self.assertEqual(len(self.slots()), 2)  # missing, not forgotten


class CleanExits(Base):
    """A window that went without the keyboard, when something can tell how its app ended."""
    claudes = Relaunching.claudes

    def setUp(self):
        super().setUp()
        self.ended = []          # what the crash reporter (and our own launches) say ended
        self.watched = True      # a crash reporter is readable
        self.ctx.endings = lambda since: ([e for e in self.ended if e.t >= since], self.watched)

    def settings(self):
        w = self.desk.open("com.system76.CosmicSettings", "Wi-Fi - COSMIC Settings", 2)
        self.engine()
        self.settle_calm()
        self.advance(3)
        return w, self.e.bind[w]

    def test_an_app_that_ended_cleanly_is_not_started_again(self):
        """COSMIC Settings closed from the panel (2026-10-03 19:16): it went without the keyboard,
        was taken as a crash and started again three times. Nothing crashed."""
        w, sid = self.settings()
        self.desk.crash(w)  # (our last look saw it without the keyboard)
        self.advance(eng.DELETE_GRACE_S + 30)
        self.assertEqual(self.desk.launched, [])
        self.assertIsNone(self.layout.find(sid)[1])
        self.assertIn("ended without crashing", next(f["why"] for k, f in self.log if k == "closed"))

    def test_an_app_the_crash_reporter_caught_comes_back(self):
        w, sid = self.settings()
        self.desk.crash(w)
        self.ended.append(Ended(t=self.clock.t + 0.4, pid=4242, exe="cosmic-settings", crashed=True))
        self.advance(eng.RELAUNCH_AFTER_S + 20)
        self.assertEqual([l.get("app") for l in self.desk.launched], ["com.system76.CosmicSettings"])
        self.assertIn("cosmic-settings crashed", next(f["why"] for k, f in self.log if k == "unbound"))

    def test_another_apps_crash_says_nothing_about_it(self):
        w, sid = self.settings()
        self.desk.crash(w)
        self.ended.append(Ended(t=self.clock.t + 0.4, pid=4242, exe="gvfsd-mtp", crashed=True))
        self.advance(eng.DELETE_GRACE_S + 30)
        self.assertEqual(self.desk.launched, [])
        self.assertIsNone(self.layout.find(sid)[1])

    def test_without_a_crash_reporter_the_keyboard_decides(self):
        self.watched = False
        w, sid = self.settings()
        self.desk.crash(w)
        self.advance(eng.RELAUNCH_AFTER_S + 20)
        self.assertEqual(len(self.desk.launched), 1)

    def test_an_app_quit_with_its_windows_open_is_kept_but_not_reopened(self):
        wins = [self.desk.open("org.gnome.Nautilus", d, 2) for d in ("tmp", "src")]
        self.engine()
        self.settle_calm()
        self.advance(3)
        self.desk.close(wins[0])  # Ctrl+Q: the keyboard passes from one to the next as they go
        self.desk.crash(wins[1])
        self.advance(eng.DELETE_GRACE_S + 30)
        self.assertEqual(self.desk.launched, [])
        self.assertEqual(len(self.slots()), 2)  # back at the next login

    def test_windows_going_together_with_a_crash_come_back(self):
        """Xwayland crashing takes every X11 terminal with it: theirs is not the name on record."""
        wins = self.claudes()
        for w in wins:
            self.desk.crash(w)
        self.ended.append(Ended(t=self.clock.t + 0.3, pid=777, exe="Xwayland", crashed=True))
        self.advance(eng.RELAUNCH_AFTER_S + 40)
        self.assertEqual(len(self.desk.launched), 2)

    def test_a_terminal_we_started_that_exited_by_itself_stays_closed(self):
        self.watched = False  # its exit status says it, crash reporter or not
        wins = self.claudes()
        for w in wins:
            self.desk.crash(w)
        self.advance(eng.RELAUNCH_AFTER_S + 40)  # (both crashed: resumed)
        self.assertEqual(len(self.desk.launched), 2)
        mine = next(w for w in self.desk.windows if w not in wins)
        pid = self.e.launched_pid[mine]
        self.desk.crash(mine)
        self.ended.append(Ended(t=self.clock.t + 0.8, pid=pid, exe="terminator", crashed=False))
        self.advance(eng.DELETE_GRACE_S + 30)
        self.assertEqual(len(self.desk.launched), 2)
        self.assertIn("exited by itself", next(f["why"] for k, f in self.log if k == "closed"))

    def test_a_terminal_we_started_that_was_killed_comes_back(self):
        self.watched = False
        wins = self.claudes()
        for w in wins:
            self.desk.crash(w)
        self.advance(eng.RELAUNCH_AFTER_S + 40)
        mine = next(w for w in self.desk.windows if w not in wins)
        pid = self.e.launched_pid[mine]
        self.desk.crash(mine)
        self.ended.append(Ended(t=self.clock.t + 0.8, pid=pid, exe="terminator", crashed=True))
        self.advance(eng.RELAUNCH_BACKOFF_S[1] + eng.RELAUNCH_AFTER_S + 20)
        self.assertEqual(len(self.desk.launched), 3)


class History(Base):
    def workspace(self):
        """Workspace 2: a Claude, a browser, a folder."""
        self.ctx.known_dirs = ["/tmp"]
        wins = [self.desk.open("terminator", "✳ Pricing", 2, claude=claude("sess-p", "Pricing", "/tmp")),
                self.desk.open("google-chrome", "Sheets - Google Chrome", 2),
                self.desk.open("org.gnome.Nautilus", "tmp", 2)]
        self.engine()
        self.settle_calm()
        self.advance(3)
        return self.layout.ws(self.e.owner[2]), wins

    def close_all(self, lw, wins):
        for w in self.e.remove_workspace(lw.id, self.clock.t, "closed by you"):
            self.desk.close(w)

    def test_a_closed_workspace_is_in_the_history(self):
        lw, wins = self.workspace()
        self.e.rename(lw.id, "Pricing")
        self.close_all(lw, wins)
        self.advance(3)
        entry = self.layout.history[0]
        self.assertEqual(entry["workspace"]["name"], "Pricing")
        self.assertEqual([s["kind"] for s in entry["workspace"]["slots"]], ["terminal", "browser", "files"])
        self.assertNotIn("window", entry["workspace"]["slots"][0])

    def test_a_workspace_emptied_window_by_window_is_in_the_history(self):
        lw, wins = self.workspace()
        self.desk.active = 1
        for w in wins:
            self.desk.close(w)
            self.advance(5)
        self.advance(eng.DELETE_GRACE_S + eng.EMPTY_DROP_S + 3)
        self.assertIsNone(self.layout.ws(lw.id))
        self.assertEqual(len(self.layout.history), 1)
        self.assertEqual({s["kind"] for s in self.layout.history[0]["workspace"]["slots"]},
                         {"terminal", "browser", "files"})

    def test_restoring_brings_the_workspace_back_and_starts_its_windows(self):
        lw, wins = self.workspace()
        self.close_all(lw, wins)
        self.advance(3)
        entry = self.layout.history[0]["id"]
        back = self.e.restore_from_history(entry, self.clock.t)
        self.advance(40)
        self.assertEqual(self.layout.history, [])
        self.assertIn(back, self.layout.workspaces)
        self.assertEqual(self.layout.recent, [])  # its Claude is back, not recent any more
        commands = [l.get("command") for l in self.desk.launched if l["type"] == "terminal"]
        self.assertEqual(commands, ["claude --resume sess-p"])
        self.assertTrue(any(l.get("paths") == ["/tmp"] for l in self.desk.launched))
        space = self.e.primary[back.id].index
        self.assertTrue(all(self.desk.windows[w]["space"] == space for w in self.e.bind if w in self.desk.windows))

    def test_a_claude_already_open_elsewhere_is_not_resumed_twice(self):
        lw, wins = self.workspace()
        self.close_all(lw, wins)
        self.advance(3)
        self.desk.open("terminator", "✳ Pricing", 1, claude=claude("sess-p", "Pricing", "/tmp"))
        self.advance(5)
        back = self.e.restore_from_history(self.layout.history[0]["id"], self.clock.t)
        self.assertFalse(any(s.session for s in back.slots))

    def test_history_survives_a_save(self):
        lw, wins = self.workspace()
        self.close_all(lw, wins)
        again = Layout.from_json(self.layout.to_json())
        self.assertEqual(again.history, self.layout.history)


if __name__ == "__main__":
    unittest.main()
