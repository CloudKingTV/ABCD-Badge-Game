"""Plays Kinlings on simulated badges and checks what it does.

    tools/sim/build.sh <path to a solana-os checkout>     (once)
    python3 tools/sim/test_game.py [folder for screenshots]

Each check boots fresh badges on the badge's own Lua with the firmware's
48 KB / 200,000-instruction limits (see badgesim.c). Memory is read from a
copy of the app that has one extra function for measuring it.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
HEAP = 48 * 1024

# The largest single block the app can still get. A failed allocation makes
# Lua collect everything first, so the heap minus this is what the app holds.
PROBE = """
function dbg_free()
  local lo, hi = 0, 380
  while lo < hi do
    local mid = (lo + hi + 1) // 2
    local ok, c = pcall(ui.canvas, 0, 0, 64, mid)
    if ok then c:remove() lo = mid else hi = mid - 1 end
  end
  return 88 + 128 * lo
end
"""

tmp = tempfile.mkdtemp(prefix="kinlings-")
APP = os.path.join(tmp, "app")
shutil.copytree(os.path.join(ROOT, "app"), APP)
with open(os.path.join(APP, "main.lua"), "a") as f:
    f.write(PROBE)
# Test members: badges AAAAAA (crown) and BBBBBB (halo).
with open(os.path.join(APP, "data", "members.txt"), "w") as f:
    f.write("# test members\nAAAAAA1 a crown\nBBBBBB2 a halo\n")

results = []
worst = {"held": 0, "steps": 0}


def state(b):
    return json.loads(b.storage).get("s", {})


def held(b):
    """Bytes of Lua heap the app holds right now."""
    h = HEAP - b.call("dbg_free")
    worst["held"] = max(worst["held"], h)
    return h


def shot(b, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        img = sim.side_by_side([x.render() for x in b]) if isinstance(b, list) else b.render()
        img.save(os.path.join(SHOTS, name + ".png"))


def saved(sp=1, g=0, bk="12", people=0, **kw):
    """A saved game with `people` in the pack, the first g of whom fed us."""
    # fo: the test badges count as foraged from already, so baskets only change by swapping
    s = dict(sp=sp, g=g, bk=bk, it="", w="", fo="7K2Q9M8ZZZZZAAAAAA9NOAPP", lf=1_794_000_000,
             ids="".join("P%05d" % i for i in range(people)),
             fl="".join("1" if i < g else "0" for i in range(people)),
             ps="".join(str(i % 4 + 1) for i in range(people)),
             nm="".join("@person%05d\n" % i for i in range(people)))
    s.update(kw)
    return json.dumps({"s": s})


def hatched(world, bid, owner, **kw):
    b = world.badge(bid, owner, APP, storage=saved(**kw)).boot()
    return b


def swap(w, a, b, accept=True):
    """a asks b (the first row of a's swap list must be b)."""
    w.advance(4000)                 # hellos
    a.key("select")
    lst = a.focused_list()
    names = [r["title"] for r in lst["items"]]
    assert b.owner in names, (names, b.owner)
    for _ in range(names.index(b.owner)):
        a.key("down")
    a.key("select")
    w.advance(300)
    assert b.modal, "no question on the other badge"
    b.key("select" if accept else "back")
    w.advance(600)


def check(fn):
    w = sim.World(seed=len(results) + 1)
    try:
        fn(w)
        for b in w.badges:
            worst["steps"] = max(worst["steps"], b.steps_peak)
        results.append((fn.__name__, "ok", ""))
    except Exception as e:  # noqa: BLE001
        results.append((fn.__name__, "FAIL", "%s: %s" % (type(e).__name__, e)))
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------

def hatching(w):
    a = w.badge("7K2Q9M", "@alice", APP).boot()
    assert a.objs and a.hint.startswith("SELECT warm"), a.hint
    shot(a, "1-egg")
    for _ in range(4):
        a.key("select")
    assert "sp" not in state(a)
    a.key("select")
    s = state(a)
    assert s["sp"] in (1, 2, 3, 4) and len(s["bk"]) == 2 and s["g"] == 0, s
    w.advance(1200)
    assert a.hint.startswith("SELECT swap")
    shot(a, "2-hatched")
    # the same badge hatches the same species
    b = w.badge("7K2Q9M", "@alice", APP).boot()
    for _ in range(5):
        b.key("select")
    assert state(b)["sp"] == s["sp"]


def back_exits_from_home_only(w):
    a = hatched(w, "7K2Q9M", "@alice")
    for k in ("up", "down", "select"):
        a.key(k)
        a.key("back")
        assert a.running, k
    a.key("back")
    assert not a.running


def foraging(w):
    a = hatched(w, "7K2Q9M", "@alice", bk="", fo="")
    others = [w.badge("X%05d" % i, "@p%d" % i) for i in range(9)]   # badges without the app
    for o in others:
        w.set_near(a, o, -40 - others.index(o))
    w.advance(40_000)
    s = state(a)
    assert len(s["bk"]) == 6, s["bk"]              # full, and no more
    assert len(s["fo"]) == 36, s["fo"]             # one snack a badge
    assert "SNACKS 6/6" in [o.get("text") for o in a.objs.values()]
    # alone with an empty basket: one turns up after two minutes
    b = hatched(w, "8ZZZZZ", "@bob", bk="", fo="")
    w.advance(125_000)
    assert len(state(b)["bk"]) == 1, state(b)


def first_swap(w):
    a = hatched(w, "7K2Q9M", "@alice", sp=1, bk="1")
    b = hatched(w, "8ZZZZZ", "@bob", sp=2, bk="3")
    c = w.badge("9NOAPP", "@carol")               # nearby, without the app
    w.all_near()
    w.advance(4000)
    a.key("select")
    rows = a.focused_list()["items"]
    assert [r["title"] for r in rows] == ["@bob", "@carol"], rows
    assert rows[0]["right"] == "new" and rows[1]["right"] == "no app yet", rows
    shot(a, "3-swap-list")
    a.key("select")
    w.advance(300)
    assert b.modal and "@alice wants to swap" in b.modal["text"], b.modal
    shot([a, b], "4-asking")
    b.key("select")
    w.advance(600)
    sa, sb = state(a), state(b)
    assert sa["g"] == 1 and sb["g"] == 1, (sa, sb)
    assert sa["bk"] == "" and sb["bk"] == "", (sa["bk"], sb["bk"])
    assert sa["ids"] == "8ZZZZZ" and sa["fl"] == "1" and sa["ps"] == "2" and sa["nm"] == "@bob\n", sa
    assert sb["ids"] == "7K2Q9M" and sb["fl"] == "1" and sb["ps"] == "1" and sb["nm"] == "@alice\n", sb
    assert a.toasts[-1][1] == "Yum! @bob joined your pack", a.toasts
    shot([a, b], "5-swapped")
    # again: friends now, nothing to grow on, and no snacks left to give
    swap(w, a, b)
    assert state(a)["g"] == 1 and state(b)["g"] == 1
    assert a.toasts[-1][1] == "No snacks to swap. Go forage!", a.toasts
    assert state(a)["ids"] == "8ZZZZZ"


def one_sided_gift(w):
    a = hatched(w, "7K2Q9M", "@alice", bk="2")
    b = hatched(w, "8ZZZZZ", "@bob", bk="")
    w.all_near()
    swap(w, a, b)
    assert state(a)["g"] == 0 and state(b)["g"] == 1
    assert state(a)["bk"] == "" and a.toasts[-1][1] == "You fed @bob", a.toasts
    assert state(a)["fl"] == "0" and state(b)["fl"] == "1"
    # later bob has one for alice: now she grows
    b.close()
    b2 = w.badge("8ZZZZZ", "@bob", APP, storage=json.dumps({"s": dict(state(b), bk="4")})).boot()
    w.badges.remove(b)
    swap(w, a, b2)
    assert state(a)["g"] == 1 and state(a)["fl"] == "1" and state(b2)["g"] == 1, (state(a), state(b2))


def declined(w):
    a = hatched(w, "7K2Q9M", "@alice", bk="2")
    b = hatched(w, "8ZZZZZ", "@bob", bk="3")
    w.all_near()
    swap(w, a, b, accept=False)
    assert state(a)["bk"] == "2" and state(b)["bk"] == "3" and state(a)["ids"] == ""
    assert a.toasts[-1][1] == "Maybe later", a.toasts


def growing_up(w):
    a = hatched(w, "7K2Q9M", "@alice", sp=4, g=1, people=1, bk="1")
    b = hatched(w, "8ZZZZZ", "@bob", sp=3, bk="2")
    w.all_near()
    shot(a, "6-before-growing")
    swap(w, a, b)
    assert state(a)["g"] == 2 and a.toasts[-1][1] == "Glim grew up!", a.toasts
    assert any(o.get("text") == "Glim the Junior" for o in a.objs.values())
    shot(a, "7-grown")


def abcd_member(w):
    m = hatched(w, "AAAAAA", "@founder", sp=3, bk="1", w=None)     # w unset: a first launch
    a = hatched(w, "7K2Q9M", "@alice", sp=1, bk="2")
    w.all_near()
    assert any(o.get("text") == "Nimbu the Hatchling  [ABCD]" for o in m.objs.values())
    n = sum(1 for o in m.objs.values() if o["kind"] == "image")
    assert n == 2, n                # the member wears their own
    swap(w, a, m)
    sa = state(a)
    assert sa["it"] == "1" and sa["w"] == "1" and sa["fl"] == "3", sa
    assert a.toasts[-1][1] == "@founder gave you a crown!", a.toasts
    imgs = [o for o in a.objs.values() if o["kind"] == "image"]
    assert len(imgs) == 4, [(o["x"], o["y"]) for o in imgs]     # the creature, its crown, the member's mini, its crown
    shot([a, m], "8-abcd-member")
    # LEFT / RIGHT: take it off, put it back on
    a.key("right")
    assert state(a)["w"] == ""
    a.key("right")
    assert state(a)["w"] == "1"
    # meeting the same member again gives nothing new
    swap(w, a, m)
    assert state(a)["it"] == "1"


def lost_messages(w):
    """Half of all radio messages vanish: a swap still happens once, or not at all."""
    done = 0
    for seed in range(12):
        w2 = sim.World(seed=100 + seed)
        a = hatched(w2, "7K2Q9M", "@alice", bk="12")
        b = hatched(w2, "8ZZZZZ", "@bob", bk="34")
        w2.all_near()
        w2.advance(8000)
        w2.loss = 0.5
        a.key("select")
        if not a.focused_list()["items"]:
            w2.close()
            continue
        a.key("select")
        for _ in range(40):
            w2.advance(250)
            if b.modal:
                b.key("select")
        w2.loss = 0
        w2.advance(3000)
        sa, sb = state(a), state(b)
        assert sa["g"] in (0, 1) and sb["g"] in (0, 1), (sa, sb)
        assert len(sa["bk"]) in (1, 2) and len(sb["bk"]) in (1, 2), (sa["bk"], sb["bk"])
        assert not (sa["g"] == 1 and sb["g"] == 0), "alice ate but bob never heard"
        done += sa["g"]
        w2.close()
    assert done >= 6, "only %d of 12 swaps got through" % done


def asking_each_other(w):
    a = hatched(w, "7K2Q9M", "@alice", bk="1")
    b = hatched(w, "8ZZZZZ", "@bob", bk="2")
    w.all_near()
    w.advance(4000)
    a.key("select")
    b.key("select")
    a.key("select")
    b.key("select")                 # both ask before either hears the other
    w.advance(400)
    assert bool(a.modal) != bool(b.modal), (a.modal, b.modal)   # one of them gets the question
    (a if a.modal else b).key("select")
    w.advance(800)
    assert state(a)["g"] == 1 and state(b)["g"] == 1, (state(a), state(b))


def hostile_radio(w):
    """Another badge's app can send anything under our app id."""
    a = hatched(w, "7K2Q9M", "@alice", bk="1")
    junk = [
        {}, {"t": 5}, {"t": "h"}, {"t": "h", "s": "x", "g": {}, "n": 12}, {"t": "h", "s": 99, "g": -5, "n": "\n\x07" * 40},
        {"t": "h", "s": 2.5, "g": 1e30, "n": "x" * 150}, {"t": "q"}, {"t": "q", "x": "1"}, {"t": "q", "x": [1]},
        {"t": "a", "x": 1, "k": 3}, {"t": "n", "x": 1}, {"t": "b", "x": 1}, {"t": "zz", "x": 1},
        {"t": "q", "x": 7, "k": 99}, {"t": "q", "x": 7, "k": "2"},
    ]
    for i, m in enumerate(junk):
        a.call("on_mesh", "EVIL%02d" % (i % 3), m)
        w.advance(100)
    assert a.modal and "EVIL" in a.modal["text"], a.modal       # the one real request shows
    a.key("select")
    s = state(a)
    assert s["g"] == 0 and s["bk"] == "", s             # k = 99 is no snack: nothing to grow on
    assert all(32 <= ord(c) < 127 or c == "\n" for c in s["nm"]), repr(s["nm"])
    # an answer nobody asked for changes nothing
    a.call("on_mesh", "EVIL00", {"t": "a", "x": 7, "k": 1})
    assert state(a)["g"] == 0
    # 30 strangers saying hello: only 16 are kept
    for i in range(30):
        a.call("on_mesh", "H%05d" % i, {"t": "h", "s": 1, "g": 1, "n": "@h%d" % i})
    a.key("select")
    assert len(a.focused_list()["items"]) == 16


def asleep_and_woken(w):
    a = hatched(w, "7K2Q9M", "@alice", bk="1", lf=1_794_000_000 - 5 * 3600)
    b = hatched(w, "8ZZZZZ", "@bob", bk="2")
    w.all_near()
    w.advance(1000)
    texts = [o.get("text") for o in a.objs.values()]
    assert "Asleep. A snack wakes it up." in texts, texts
    assert a.leds == [0] * 8
    shot(a, "9-asleep")
    swap(w, a, b)
    w.advance(4000)
    texts = [o.get("text") for o in a.objs.values()]
    assert "Asleep. A snack wakes it up." not in texts, texts
    assert a.leds[0] != 0 and a.leds[1] == 0, a.leds    # one friend nearby: one LED


def no_clock(w):
    w.epoch = None                  # a badge that never synced its clock
    a = w.badge("7K2Q9M", "@alice", APP).boot()
    for _ in range(5):
        a.key("select")
    b = hatched(w, "8ZZZZZ", "@bob", bk="2")
    w.all_near()
    swap(w, a, b)
    assert state(a)["g"] == 1


def a_full_pack(w):
    """A full pack of 150: memory on every screen, and what happens at the limit."""
    a = hatched(w, "7K2Q9M", "@alice", sp=2, g=150, people=150, bk="123412")
    b = hatched(w, "8ZZZZZ", "@bob", sp=1, bk="2")
    w.all_near()
    w.advance(3000)
    mem = {"home": held(a)}
    shot(a, "10-full-pack")
    a.key("up")
    mem["pack list"] = held(a)
    assert len(a.focused_list()["items"]) == 40
    shot(a, "11-pack-list")
    a.key("back")
    a.key("down")
    mem["help"] = held(a)
    shot(a, "12-help")
    a.key("back")
    a.key("select")
    mem["swap list"] = held(a)
    a.key("back")
    swap(w, a, b)
    mem["after a swap"] = held(a)
    s = state(a)
    assert len(s["ids"]) == 900 and s["g"] == 150, (len(s["ids"]), s["g"])    # full: not added
    assert state(b)["g"] == 1
    print("   memory held with a full pack:", ", ".join("%s %.1f KB" % (k, v / 1024) for k, v in mem.items()))
    print("   saved data: %.1f KB of 16 KB" % (len(a.storage) / 1024))
    assert max(mem.values()) < 40 * 1024, mem


def memory_when_new(w):
    a = w.badge("7K2Q9M", "@alice", APP).boot()
    mem = {"egg": held(a)}
    for _ in range(5):
        a.key("select")
    w.advance(1000)
    mem["home"] = held(a)
    a.key("select")
    mem["swap list"] = held(a)
    print("   memory held when new:", ", ".join("%s %.1f KB" % (k, v / 1024) for k, v in mem.items()))
    assert max(mem.values()) < 36 * 1024, mem


def a_long_day(w):
    """Six badges swap at random for a simulated half hour without an error."""
    bs = [hatched(w, "B%05d" % i, "@p%d" % i, sp=i % 4 + 1, bk="1234") for i in range(6)]
    w.all_near()
    for step in range(60):
        w.advance(30_000)
        a, b = w.rng.sample(bs, 2)
        if not a.running or not b.running:
            continue
        a.key("select")
        lst = a.focused_list()
        if lst and lst["items"]:
            names = [r["title"] for r in lst["items"]]
            if b.owner in names:
                for _ in range(names.index(b.owner)):
                    a.key("down")
                a.key("select")
                w.advance(400)
                if b.modal:
                    b.key("select")
                w.advance(600)
        if a.focused_list():
            a.key("back")
    for b in bs:
        s = state(b)
        n = len(s["ids"]) // 6
        assert len(s["fl"]) == n and len(s["ps"]) == n and s["nm"].count("\n") == n, s
        assert s["g"] == sum(1 for c in s["fl"] if c in "13"), s
        assert 0 <= len(s["bk"]) <= 6
    print("   fed-by counts after a busy half hour:", [state(b)["g"] for b in bs])
    assert sum(state(b)["g"] for b in bs) >= 6


for fn in (hatching, back_exits_from_home_only, foraging, first_swap, one_sided_gift, declined, growing_up,
           abcd_member, lost_messages, asking_each_other, hostile_radio, asleep_and_woken, no_clock,
           a_full_pack, memory_when_new, a_long_day):
    check(fn)

shutil.rmtree(tmp, ignore_errors=True)
print()
for name, res, why in results:
    print("%-28s %s %s" % (name, res, why))
print("\nmost memory held: %.1f KB of 48 KB; most instructions in one callback: %d of 200000"
      % (worst["held"] / 1024, worst["steps"]))
sys.exit(1 if any(r[1] != "ok" for r in results) else 0)
