"""Drives tools/sim/badgesim: a badge's screen, LEDs, buttons, radio and clock.

badgesim runs the app on the badge's own Lua with the firmware's memory and
instruction limits; this module plays everything else, following the rules in
Solana OS's main/lua/*.c (object and timer limits, list keys, mesh size and
rate limits). A World holds several badges on one virtual clock, so two or
more copies of the app can talk over the simulated radio.

It is a test double, not the firmware: fonts and exact pixel layout differ,
and nothing here replaces a run in the real emulator or on a badge.
"""

import json
import os
import random
import subprocess

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "badgesim")

W, H, STATUS_H, HINT_H, ROW_H = 320, 218, 22, 16, 36
OBJ_MAX, TIMER_MAX, TIMER_MIN_MS, TEXT_MAX, TEXT_TOTAL = 64, 8, 20, 200, 6 * 1024
MESH_MAX, MESH_PER_S, PEER_MAX, TICK_MS = 200, 4, 16, 50

THEME = {
    "text": 0xFFFFFF, "dim": 0x8E8C9E, "bg": 0x000000, "panel": 0x111116, "border": 0x2A2935,
    "purple": 0x9945FF, "green": 0x14F195, "pink": 0xEB54BC, "lpurple": 0xB6B0FF, "blue": 0x64A8F2,
    "aqua": 0x80ECFF, "lime": 0xACFF9B, "yellow": 0xE8F180, "orange": 0xFF754A,
}
ART = {"k": 0x000000, "w": 0xFFFFFF, "p": 0x9945FF, "g": 0x14F195, "m": 0xEB54BC, "v": 0xB6B0FF,
       "b": 0x64A8F2, "a": 0x80ECFF, "l": 0xACFF9B, "y": 0xE8F180, "o": 0xFF754A, "#": 0xFFFFFF}
CLEAR = None


class AppError(Exception):
    """The app stopped with an error, ran out of memory or took too long."""


class ApiError(Exception):
    """An API call the firmware would refuse; raised in the app as a Lua error."""


def rgb565(c):
    """The colour the 16-bit screen shows for 0xRRGGBB."""
    r, g, b = (c >> 16 & 0xFF) >> 3, (c >> 8 & 0xFF) >> 2, (c & 0xFF) >> 3
    return ((r << 3 | r >> 2) << 16) | ((g << 2 | g >> 4) << 8) | (b << 3 | b >> 2)


def color(v):
    if isinstance(v, bool) or v is None:
        raise ApiError("colour must be a theme name or 0xRRGGBB, not '%s'" % ("boolean" if v is not None else "nil"))
    if isinstance(v, int):
        return v & 0xFFFFFF
    if isinstance(v, str) and v in THEME:
        return THEME[v]
    raise ApiError("colour must be a theme name or 0xRRGGBB, not '%s'" % v)


def integer(v, what="number"):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise ApiError("bad argument (%s expected)" % what)
    return int(v)


def clip_text(t):
    if not isinstance(t, (str, int, float)) or isinstance(t, bool):
        raise ApiError("bad argument (string expected)")
    t = str(t)
    b = t.encode()
    return t if len(b) <= TEXT_MAX else b[:TEXT_MAX - 3].decode(errors="ignore") + "..."


def opts_of(v):
    return v if isinstance(v, dict) else {}


class World:
    """Badges that share a clock and a radio."""

    def __init__(self, seed=1, epoch=1_794_000_000):
        self.now = 0                # ms
        self.epoch = epoch          # unix seconds at now = 0; None: clock never synced
        self.badges = []
        self.near = {}              # frozenset({a, b}) -> rssi
        self.queue = []             # (due, seq, to badge, from id, message)
        self.seq = 0
        self.loss = 0.0             # share of radio messages dropped
        self.rng = random.Random(seed)

    def badge(self, *a, **kw):
        b = Badge(self, *a, **kw)
        self.badges.append(b)
        return b

    def set_near(self, a, b, rssi=-50):
        if rssi is None:
            self.near.pop(frozenset((a.id, b.id)), None)
        else:
            self.near[frozenset((a.id, b.id))] = rssi

    def all_near(self, rssi=-50):
        for i, a in enumerate(self.badges):
            for b in self.badges[i + 1:]:
                self.set_near(a, b, rssi)

    def peers(self, me):
        out = []
        for b in self.badges:
            rssi = self.near.get(frozenset((me.id, b.id)))
            if b is not me and rssi is not None and b.radio_on:
                out.append((rssi, b))
        out.sort(key=lambda p: -p[0])
        return out[:PEER_MAX]

    def send(self, src, to, msg):
        for _, b in self.peers(src):
            if (to == "*" or to == b.id) and b.running and b.app_id == src.app_id:
                if self.rng.random() < self.loss:
                    continue
                self.seq += 1
                self.queue.append((self.now + 8, self.seq, b, src.id, msg))

    def advance(self, ms, step=10):
        """Runs every badge for ms of virtual time."""
        end = self.now + ms
        while self.now < end:
            self.now += step
            due = sorted(q for q in self.queue if q[0] <= self.now)
            self.queue = [q for q in self.queue if q[0] > self.now]
            for _, _, b, src, msg in due:
                if b.running:
                    b.call("on_mesh", src, msg)
            for b in self.badges:
                if b.running:
                    b.run_clock()

    def close(self):
        for b in self.badges:
            b.close()


class Badge:
    def __init__(self, world, badge_id, owner=None, app_dir=None, level=1, storage="{}", seed=None):
        self.world = world
        self.id = badge_id
        self.owner = owner
        self.level = level
        self.app_dir = app_dir
        self.storage = storage
        self.seed = seed if seed is not None else world.rng.randrange(1, 1 << 30)
        self.radio_on = True        # sends presence whether or not the app runs
        self.running = False
        self.proc = None
        self.app_id = None
        self.perms = []
        self.prints = []
        self.mem = self.peak = self.steps_peak = 0
        self.boot_mem = 0
        self._reset()

    def _reset(self):
        self.objs = {}
        self.next_id = 1
        self.order = 0
        self.text_total = 0
        self.bg = THEME["bg"]
        self.hint = ""
        self.toasts = []            # (time, text)
        self.modal = None           # {"kind": "confirm"|"input", "text", "ref"}
        self.leds = [0] * 8
        self.timers = {}
        self.timer_seq = 0
        self.mesh_sent = [None] * MESH_PER_S
        self.exit_req = False
        self.next_tick = 0
        self.t0 = 0
        self.unrefs = []
        self.sent_log = []          # (time, to, message, bytes) of every mesh.send that went out
        self.last_steps = 0

    # ---- the process -------------------------------------------------------

    def boot(self):
        manifest = json.load(open(os.path.join(self.app_dir, "app.json")))
        self.app_id = manifest["id"]
        self.perms = manifest.get("permissions", [])
        self._reset()
        self.proc = subprocess.Popen([SIM], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.running = True
        self.t0 = self.world.now
        self.next_tick = self.world.now + TICK_MS
        self._run({"op": "boot", "dir": self.app_dir, "storage": self.storage, "seed": self.seed})
        self.boot_mem = self.mem
        return self

    def _run(self, op):
        self.proc.stdin.write(json.dumps(op) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                self.running = False
                raise AppError("%s: badgesim stopped" % self.id)
            m = json.loads(line)
            if "api" in m:
                try:
                    ret, steps = self._api(m["api"], m["args"])
                    reply = {"n": len(ret), "ret": ret, "steps": steps}
                except ApiError as e:
                    reply = {"err": str(e)}
                self.proc.stdin.write(json.dumps(reply) + "\n")
                self.proc.stdin.flush()
                continue
            self.mem, self.peak, self.steps_peak = m["mem"], m["peak"], m["steps_peak"]
            self.last_steps = m["steps"]
            for ref in self.unrefs:
                self.proc.stdin.write(json.dumps({"op": "unref", "ref": ref}) + "\n")
            self.unrefs = []
            self.proc.stdin.flush()
            if not m["ok"]:
                self.running = False
                kind = "Out of memory" if m.get("oom") else "Script took too long" if m.get("killed") else "Script error"
                raise AppError("%s: %s: %s" % (self.id, kind, m.get("err")))
            return None if m.get("nofn") else m.get("ret")

    def call(self, fn, *args):
        return self._run({"op": "call", "fn": fn, "args": json.dumps(list(args)), "n": len(args)})

    def callref(self, ref, *args, unref=False):
        return self._run({"op": "callref", "ref": ref, "args": json.dumps(list(args)), "n": len(args),
                          "unref": unref})

    def close(self):
        if self.proc and self.running:
            self.running = False
            self.proc.stdin.write('{"op":"close"}\n')
            self.proc.stdin.flush()
            self.running = True
            try:
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        break
                    m = json.loads(line)
                    if "api" in m:
                        try:
                            ret, steps = self._api(m["api"], m["args"])
                            reply = {"n": len(ret), "ret": ret, "steps": steps}
                        except ApiError as e:
                            reply = {"err": str(e)}
                        self.proc.stdin.write(json.dumps(reply) + "\n")
                        self.proc.stdin.flush()
                    else:
                        if not m["ok"]:
                            self.prints.append("on_exit failed: %s" % m.get("err"))
                        break
            finally:
                self.running = False
            self.proc.wait(timeout=5)
        self.proc = None
        self.leds = [0] * 8

    # ---- the clock: ticks and timers, in the firmware's order ---------------

    def run_clock(self):
        now = self.world.now
        if self.running and now >= self.next_tick:
            ms = now - self.next_tick + TICK_MS
            self.next_tick = now + TICK_MS
            self.call("on_tick", ms)
        for _ in range(TIMER_MAX):
            if not self.running or self.exit_req:
                break
            due = [t for t in self.timers.values() if t["due"] <= now]
            if not due:
                break
            t = min(due, key=lambda t: t["due"])
            if t["period"]:
                t["due"] += t["period"]
                if t["due"] <= now:
                    t["due"] = now + t["period"]
                self.callref(t["ref"])
            else:
                del self.timers[t["id"]]
                self.callref(t["ref"], unref=True)
        self.toasts = [t for t in self.toasts if now - t[0] < 2500]
        if self.exit_req:
            self.close()

    # ---- keys, as lua_ui.c's lh_ui_key and lua_host.c's key_cb ---------------

    def key(self, name, rep=False):
        assert name in ("up", "down", "left", "right", "select", "back"), name
        if not self.running:
            return
        if self.modal:
            if self.modal["kind"] == "confirm" and (name == "select" or (name == "back" and not rep)):
                m, self.modal = self.modal, None
                self.callref(m["ref"], name == "select", unref=True)
            return
        lst = self.focused_list()
        if lst:
            n = len(lst["items"])
            if name in ("up", "down"):
                if n:
                    s = lst["sel"] + (1 if name == "down" else -1)
                    if s < 0:
                        s = 0 if rep else n - 1
                    elif s >= n:
                        s = n - 1 if rep else 0
                    lst["sel"] = s
                return
            if name == "select" and lst["on_select"] is not None and n:
                self.callref(lst["on_select"], lst["sel"] + 1, lst["items"][lst["sel"]])
                if self.exit_req:
                    self.close()
                return
        handled = self.call("on_key", name, rep)
        if not handled and name == "back" and not rep:
            self.exit_req = True
        if self.exit_req:
            self.close()

    def input(self, text):
        """Answers an open ui.input (None: cancelled)."""
        assert self.modal and self.modal["kind"] == "input"
        m, self.modal = self.modal, None
        self.callref(m["ref"], text, unref=True)

    def focused_list(self):
        lists = [o for o in self.objs.values() if o["kind"] == "list" and not o["hidden"]]
        return max(lists, key=lambda o: o["order"]) if lists else None

    # ---- the API -----------------------------------------------------------

    def _api(self, name, args):
        if name == "print":
            self.prints.append(args[0])
            return [], 0
        if name == "storage.save":
            self.storage = args[0]
            return [], 0
        if ":" in name:
            kind, method = name.split(":")
            fn = getattr(self, "m_" + method, None)
            if not fn:
                raise ApiError("attempt to call a nil value (method '%s')" % method)
            if not args or not isinstance(args[0], dict) or "__obj" not in args[0]:
                raise ApiError("bad argument #1 (UI object expected)")
            o = self.objs.get(args[0]["__obj"])
            if kind == "ui.img":
                if not o:
                    raise ApiError("bad argument #1 (image expected)")
            elif not o or o.get("removed"):
                if method == "remove":
                    return [], 0
                raise ApiError("this object was removed")
            r = fn(o, *args[1:])
        else:
            fn = getattr(self, name.replace(".", "_"), None)
            if not fn:
                raise ApiError("attempt to call a nil value (field '%s')" % name.split(".")[1])
            r = fn(*args)
        steps = 0
        if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], _Steps):
            r, steps = r[0], r[1].n
        if r is None:
            r = []
        elif not isinstance(r, list):
            r = [r]
        return r, steps

    def need(self, perm):
        if perm not in self.perms:
            raise ApiError('%s: add the "%s" permission to app.json' % (perm, perm))

    def new_obj(self, kind, meta, bytes_, **fields):
        if kind != "pix" and sum(1 for o in self.objs.values() if o["kind"] != "pix") >= OBJ_MAX:
            raise ApiError("too many UI objects (%d)" % OBJ_MAX)
        self.order += 1
        o = dict(id=self.next_id, kind=kind, order=self.order, hidden=False, **fields)
        self.next_id += 1
        self.objs[o["id"]] = o
        return {"__new": {"id": o["id"], "kind": meta, "bytes": bytes_, "uv": 0}}

    def obj_arg(self, v):
        return self.objs.get(v["__obj"]) if isinstance(v, dict) and "__obj" in v else None

    # ui

    def ui_bg(self, c=None):
        self.bg = color(c)

    def check_text(self, old, new):
        if self.text_total - old + new > TEXT_TOTAL:
            raise ApiError("too much text on screen (%d KB per app)" % (TEXT_TOTAL // 1024))
        self.text_total += new - old

    def ui_label(self, x=None, y=None, text=None, opts=None):
        o = opts_of(opts)
        text = clip_text(text)
        size = o.get("size", 14)
        if size not in (12, 14, 20, 32):
            raise ApiError("size must be 12, 14, 20 or 32")
        c = color(o["color"]) if "color" in o else THEME["text"]
        self.check_text(0, len(text.encode()))
        return self.new_obj("label", "ui.label", 4, x=integer(x), y=integer(y), text=text, size=size,
                            mono=bool(o.get("mono")) or size == 12, color=c, w=integer(o.get("w", 0)),
                            align=o.get("align", "left"))

    def ui_rect(self, x=None, y=None, w=None, h=None, opts=None):
        o = opts_of(opts)
        return self.new_obj("rect", "ui.rect", 4, x=integer(x), y=integer(y), w=integer(w), h=integer(h),
                            color=color(o["color"]) if "color" in o else THEME["panel"],
                            radius=integer(o.get("radius", 0)),
                            border=color(o["border"]) if "border" in o else None)

    def ui_bar(self, x=None, y=None, w=None, h=None, value=0):
        return self.new_obj("bar", "ui.bar", 4, x=integer(x), y=integer(y), w=integer(w), h=integer(h),
                            value=max(0, min(100, integer(value))))

    def ui_list(self, x=None, y=None, w=None, h=None, items=None, opts=None):
        o = opts_of(opts)
        if not isinstance(items, (list, dict)):
            raise ApiError("bad argument #5 to 'list' (table expected)")
        sel = o.get("on_select")
        if sel is not None and not (isinstance(sel, dict) and "__fn" in sel):
            raise ApiError("on_select must be a function")
        items = items if isinstance(items, list) else []
        return self.new_obj("list", "ui.list", 4, x=integer(x), y=integer(y), w=integer(w), h=integer(h),
                            items=items, sel=0 if items else -1, on_select=sel["__fn"] if sel else None)

    def ui_hint(self, text=""):
        self.hint = clip_text(text or "")

    def ui_toast(self, text=None):
        self.toasts.append((self.world.now, clip_text(text)))

    def dialog(self, kind, text, fn):
        if not (isinstance(fn, dict) and "__fn" in fn):
            raise ApiError("bad argument (function expected)")
        if self.modal:
            raise ApiError("a dialog is already open")
        self.modal = {"kind": kind, "text": clip_text(text), "ref": fn["__fn"]}

    def ui_confirm(self, text=None, fn=None):
        self.dialog("confirm", text, fn)

    def ui_input(self, title=None, maxlen=None, fn=None):
        integer(maxlen)
        self.dialog("input", title, fn)

    def ui_clear(self):
        for o in list(self.objs.values()):
            if o["kind"] != "pix":
                self.forget(o)
        self.text_total = 0
        self.hint = ""

    def m_pos(self, o, x=None, y=None):
        o["x"], o["y"] = integer(x), integer(y)

    def m_show(self, o, show=True):
        o["hidden"] = not (show is None or show)

    def m_remove(self, o):
        if o["kind"] == "label":
            self.text_total -= len(o["text"].encode())
        self.forget(o)

    def forget(self, o):
        """An object goes, and with it the firmware's hold on its on_select function."""
        o["removed"] = True
        self.objs.pop(o["id"], None)
        if o.get("on_select") is not None:
            self.unrefs.append(o["on_select"])

    def kind(self, o, *kinds):
        if o["kind"] not in kinds:
            raise ApiError("bad argument #1 (%s expected)" % kinds[0])

    def m_text(self, o, text=None):
        self.kind(o, "label")
        text = clip_text(text)
        self.check_text(len(o["text"].encode()), len(text.encode()))
        o["text"] = text

    def m_color(self, o, c=None):
        o["color"] = color(c)

    def m_size(self, o, w=None, h=None):
        if o["kind"] == "pix":
            return [o["w"], o["h"]]
        self.kind(o, "rect", "bar")
        o["w"], o["h"] = integer(w), integer(h)

    def m_items(self, o, items=None):
        self.kind(o, "list")
        if not isinstance(items, (list, dict)):
            raise ApiError("bad argument #2 to 'items' (table expected)")
        o["items"] = items if isinstance(items, list) else []
        n = len(o["items"])
        o["sel"] = min(o["sel"], n - 1)
        if o["sel"] < 0 and n:
            o["sel"] = 0

    def m_selected(self, o):
        self.kind(o, "list")
        return [o["sel"] + 1 if o["items"] else None]

    def m_select(self, o, i=None):
        self.kind(o, "list")
        n = len(o["items"])
        o["sel"] = max(0 if n else -1, min(integer(i) - 1, n - 1))

    # images (lua_img.c)

    def to_pix(self, src, opts, what):
        o = self.obj_arg(src)
        if o is not None:
            if o["kind"] != "pix":
                raise ApiError("bad argument (image, PNG or GIF path, or table of rows expected)")
            return o, 0, 0
        if isinstance(src, str):
            path = os.path.join(self.app_dir, src)
            if not os.path.exists(path):
                raise ApiError("%s: %s not found" % (what, src))
            im = Image.open(path).convert("RGBA")
            px = [[(None if a < 128 else rgb565(r << 16 | g << 8 | b)) for r, g, b, a in
                   (im.getpixel((x, y)) for x in range(im.width))] for y in range(im.height)]
            w, h = im.width, im.height
            steps = 500 + os.path.getsize(path) // 4
        elif isinstance(src, list):
            pal = dict(ART)
            pal["#"] = THEME["text"]
            for k, v in opts_of(opts).get("palette", {}).items():
                if len(k) != 1:
                    raise ApiError("%s: palette keys are single characters" % what)
                pal[k] = CLEAR if v is False else color(v)
            if not 1 <= len(src) <= 1024:
                raise ApiError("%s: pixel art takes 1 to 1024 rows" % what)
            for i, row in enumerate(src):
                if not isinstance(row, str):
                    raise ApiError("%s: row %d isn't a string" % (what, i + 1))
            w, h = max(len(r) for r in src), len(src)
            if w < 1:
                raise ApiError("%s: pixel art rows are 1 to 1024 characters" % what)
            px = []
            for y, row in enumerate(src):
                line = []
                for x in range(w):
                    ch = row[x] if x < len(row) else "."
                    if ch in ". ":
                        line.append(CLEAR)
                    elif ch in pal:
                        line.append(CLEAR if pal[ch] is CLEAR else rgb565(pal[ch]))
                    else:
                        raise ApiError("%s: row %d: '%s' isn't in the palette" % (what, y + 1, ch))
                px.append(line)
            steps = w * h // 16
        else:
            raise ApiError("bad argument (image, PNG or GIF path, or table of rows expected)")
        if w * h * 2 > 48 * 1024:
            raise ApiError("%s: %d x %d pixels take %d KB, more than an app's 48 KB" % (what, w, h, w * h * 2 // 1024))
        self.order += 1
        o = dict(id=self.next_id, kind="pix", order=self.order, hidden=True, w=w, h=h, px=px)
        self.next_id += 1
        self.objs[o["id"]] = o
        return o, 4 + w * h * 2, steps

    def scale(self, opts, what, pix):
        s = integer(opts_of(opts).get("scale", 1))
        if not 1 <= s <= 16:
            raise ApiError("%s: scale must be 1 to 16" % what)
        return s

    def ui_load(self, src=None, opts=None):
        o, bytes_, steps = self.to_pix(src, opts, "ui.load")
        if bytes_ == 0:
            raise ApiError("ui.load: already loaded")
        return {"__new": {"id": o["id"], "kind": "ui.img", "bytes": bytes_, "uv": 0}}, _Steps(steps)

    def ui_image(self, x=None, y=None, src=None, opts=None):
        pix, bytes_, steps = self.to_pix(src, opts, "ui.image")
        s = self.scale(opts, "ui.image", pix)
        # the handle, the view (lh_view_t) and, for an inline source, its pixels
        return self.new_obj("image", "ui.image", 4 + 60 + (bytes_ + 20 if bytes_ else 0), x=integer(x),
                            y=integer(y), pix=pix, scale=s), _Steps(steps)

    def m_set(self, o, src=None, opts=None):
        if o["kind"] == "bar":
            o["value"] = max(0, min(100, integer(src)))
            return None
        self.kind(o, "image")
        pix, bytes_, steps = self.to_pix(src, opts, "image:set")
        o["pix"] = pix
        return [], _Steps(steps)

    def m_scale(self, o, s=None):
        self.kind(o, "image")
        s = integer(s)
        if not 1 <= s <= 16:
            raise ApiError("image:scale: scale must be 1 to 16")
        o["scale"] = s

    def ui_canvas(self, x=None, y=None, w=None, h=None, opts=None):
        w, h = integer(w), integer(h)
        if not (1 <= w <= 1024 and 1 <= h <= 1024):
            raise ApiError("ui.canvas: 1 to 1024 pixels per side")
        if w * h * 2 > 48 * 1024:
            raise ApiError("ui.canvas: %d x %d pixels take %d KB, more than an app's 48 KB" % (w, h, w * h * 2 // 1024))
        o = opts_of(opts)
        s = self.scale(opts, "ui.canvas", None)
        fill = rgb565(color(o["color"])) if "color" in o else CLEAR
        pix = dict(kind="pix", w=w, h=h, px=[[fill] * w for _ in range(h)])
        return self.new_obj("canvas", "ui.canvas", 4 + 60 + 4 + w * h * 2 + 20, x=integer(x), y=integer(y),
                            pix=pix, scale=s), _Steps(w * h // 16)

    @staticmethod
    def px_arg(c):
        return CLEAR if c is None or c is False else rgb565(color(c))

    def fill(self, p, x, y, w, h, c):
        x2, y2 = min(x + w, p["w"]), min(y + h, p["h"])
        x, y = max(x, 0), max(y, 0)
        if x >= x2 or y >= y2:
            return 0
        for j in range(y, y2):
            p["px"][j][x:x2] = [c] * (x2 - x)
        return (x2 - x) * (y2 - y) // 16

    def m_clear(self, o, c=None):
        self.kind(o, "canvas")
        p = o["pix"]
        return [], _Steps(self.fill(p, 0, 0, p["w"], p["h"], self.px_arg(c)))

    def m_pixel(self, o, x=None, y=None, c=None):
        self.kind(o, "canvas")
        p, x, y = o["pix"], integer(x), integer(y)
        if 0 <= x < p["w"] and 0 <= y < p["h"]:
            p["px"][y][x] = self.px_arg(c)

    def m_rect(self, o, x=None, y=None, w=None, h=None, c=None):
        self.kind(o, "canvas")
        w, h = integer(w), integer(h)
        steps = self.fill(o["pix"], integer(x), integer(y), w, h, self.px_arg(c)) if w > 0 and h > 0 else 0
        return [], _Steps(steps)

    def m_line(self, o, x0=None, y0=None, x1=None, y1=None, c=None):
        self.kind(o, "canvas")
        p, c = o["pix"], self.px_arg(c)
        x0, y0, x1, y1 = integer(x0), integer(y0), integer(x1), integer(y1)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy, err = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1), dx + dy
        steps = max(dx, -dy) // 16
        for _ in range(4096):
            if 0 <= x0 < p["w"] and 0 <= y0 < p["h"]:
                p["px"][y0][x0] = c
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
        return [], _Steps(steps)

    def m_image(self, o, src=None, x=None, y=None, opts=None):
        self.kind(o, "canvas")
        p = o["pix"]
        s, _, steps = self.to_pix(src, opts, "canvas:image")
        x, y = integer(x), integer(y)
        i0, j0 = max(-x, 0), max(-y, 0)
        i1, j1 = min(s["w"], p["w"] - x), min(s["h"], p["h"] - y)
        if i0 >= i1 or j0 >= j1:
            return [], _Steps(steps)
        for j in range(j0, j1):
            for i in range(i0, i1):
                if s["px"][j][i] is not CLEAR:
                    p["px"][y + j][x + i] = s["px"][j][i]
        return [], _Steps(steps + (i1 - i0) * (j1 - j0) // 16)

    def m_get(self, o, x=None, y=None):
        self.kind(o, "canvas")
        p, x, y = o["pix"], integer(x), integer(y)
        return [p["px"][y][x] if 0 <= x < p["w"] and 0 <= y < p["h"] else None]

    # led, timer, sys, badge

    def led_index(self, i):
        i = integer(i)
        if not 1 <= i <= 8:
            raise ApiError("bad argument #1 (LEDs are 1..8)")
        return i - 1

    def led_set(self, i=None, r=None, g=None, b=None):
        self.need("led")
        ch = [max(0, min(255, integer(v))) for v in (r, g, b)]
        self.leds[self.led_index(i)] = ch[0] << 16 | ch[1] << 8 | ch[2]

    def led_hex(self, i=None, c=None):
        self.need("led")
        self.leds[self.led_index(i)] = integer(c) & 0xFFFFFF

    def led_fill(self, r=None, g=None, b=None):
        self.need("led")
        ch = [max(0, min(255, integer(v))) for v in (r, g, b)]
        self.leds = [ch[0] << 16 | ch[1] << 8 | ch[2]] * 8

    def led_off(self):
        self.need("led")
        self.leds = [0] * 8

    def add_timer(self, ms, fn, every):
        ms = integer(ms)
        if not (isinstance(fn, dict) and "__fn" in fn):
            raise ApiError("bad argument #2 (function expected)")
        if len(self.timers) >= TIMER_MAX:
            raise ApiError("too many timers (%d)" % TIMER_MAX)
        ms = max(ms, TIMER_MIN_MS)
        self.timer_seq += 1
        self.timers[self.timer_seq] = dict(id=self.timer_seq, ref=fn["__fn"], period=ms if every else 0,
                                           due=self.world.now + ms)
        return [self.timer_seq]

    def timer_after(self, ms=None, fn=None):
        return self.add_timer(ms, fn, False)

    def timer_every(self, ms=None, fn=None):
        return self.add_timer(ms, fn, True)

    def timer_cancel(self, tid=None):
        if tid is None:
            raise ApiError("bad argument #1 to 'cancel' (number expected, got nil)")
        t = self.timers.pop(integer(tid), None)
        if t:
            self.unrefs.append(t["ref"])     # sent once the running call is over

    def sys_millis(self):
        return [self.world.now - self.t0]

    def sys_time(self):
        return [None if self.world.epoch is None else self.world.epoch + self.world.now // 1000]

    def sys_battery(self):
        return [87]

    def sys_keys(self):
        return [{}]

    def sys_exit(self):
        self.exit_req = True

    def badge_id(self):
        return [self.id]

    def badge_owner(self):
        return [self.owner]

    def badge_online(self):
        return [False]

    # mesh (lua_net.c)

    def mesh_nearby(self):
        self.need("mesh")
        return [[dict(id=b.id, name=b.owner or "", level=b.level, rssi=rssi, kind="badge", bridged=False)
                 for rssi, b in self.world.peers(self)]]

    def mesh_send(self, to=None, msg=None):
        self.need("mesh")
        if not isinstance(to, str):
            raise ApiError("bad argument #1 to 'send' (string expected)")
        if not isinstance(msg, (dict, list)):
            raise ApiError("bad argument #2 to 'send' (table expected)")
        if to != "*" and len(to) != 6:
            raise ApiError("bad argument #1 to 'send' (a badge id or \"*\")")
        now = self.world.now
        oldest = min(range(MESH_PER_S), key=lambda i: -1 if self.mesh_sent[i] is None else self.mesh_sent[i])
        if self.mesh_sent[oldest] is not None and now - self.mesh_sent[oldest] < 1000:
            return [None, "too fast: 4 messages per second"]
        size = len(json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode())
        if size > MESH_MAX:
            return [None, "message over 200 bytes"]
        if to != "*" and not any(b.id == to for _, b in self.world.peers(self)):
            return [None, "not nearby"]
        self.mesh_sent[oldest] = now
        self.sent_log.append((now, to, msg, size))
        self.world.send(self, to, msg)
        return [True]

    # ---- a picture of the badge --------------------------------------------

    def render(self, k=2):
        return render(self, k)


class _Steps:
    def __init__(self, n):
        self.n = n


# ---------------------------------------------------------------------------
# Drawing

_fonts = {}


def font(size, mono, k):
    key = (size, mono, k)
    if key not in _fonts:
        paths = (["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"] if mono else
                 ["/usr/share/fonts/opentype/inter/Inter-Regular.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
        px = {12: 11, 14: 13, 20: 18, 32: 29}.get(size, size) * k
        f = None
        for p in paths:
            if os.path.exists(p):
                f = ImageFont.truetype(p, px)
                break
        _fonts[key] = f or ImageFont.load_default(px)
    return _fonts[key]


def rgb(c):
    return (c >> 16 & 0xFF, c >> 8 & 0xFF, c & 0xFF)


def wrap(d, text, f, width):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            t = (cur + " " + word).strip() if cur else word
            if cur and d.textlength(t, font=f) > width:
                lines.append(cur)
                cur = word
            else:
                cur = t
        lines.append(cur)
    return lines


def draw_pix(img, pix, x, y, s, k):
    px = img.load()
    for j, row in enumerate(pix["px"]):
        for i, c in enumerate(row):
            if c is CLEAR:
                continue
            x0, y0 = (x + i * s) * k, (y + STATUS_H + j * s) * k
            col = rgb(c)
            for yy in range(max(y0, STATUS_H * k), min(y0 + s * k, (STATUS_H + H) * k)):
                for xx in range(max(x0, 0), min(x0 + s * k, W * k)):
                    px[xx + _OX, yy + _OY] = col


_OX = _OY = 0


def render(b, k=2):
    """The badge as a picture: LEDs, status bar and the app's 320 x 218 area."""
    global _OX, _OY
    pad, top = 14 * k, 26 * k
    img = Image.new("RGB", (W * k + pad * 2 + 14 * k, (STATUS_H + H) * k + top + pad), (24, 24, 30))
    d = ImageDraw.Draw(img)
    _OX, _OY = pad, top
    ox, oy = _OX, _OY

    def led(i, cx, cy):
        c = b.leds[i]
        on = c != 0
        d.ellipse([cx - 5 * k, cy - 5 * k, cx + 5 * k, cy + 5 * k], fill=rgb(c) if on else (50, 50, 58),
                  outline=(90, 90, 100))
    for i in range(6):
        led(i, ox + (30 + i * 52) * k, 13 * k)
    led(6, ox + W * k + 13 * k, oy + 70 * k)
    led(7, ox + W * k + 13 * k, oy + 150 * k)

    d.rectangle([ox, oy, ox + W * k - 1, oy + (STATUS_H + H) * k - 1], fill=rgb(b.bg))
    d.rectangle([ox, oy, ox + W * k - 1, oy + STATUS_H * k - 1], fill=(17, 17, 22))
    f12 = font(12, True, k)
    d.text((ox + 6 * k, oy + 5 * k), "%s  %s" % (b.app_id or "", b.owner or b.id), font=f12, fill=rgb(THEME["dim"]))
    if not b.running:
        d.text((ox + 110 * k, oy + 110 * k), "(app closed)", font=font(14, False, k), fill=rgb(THEME["dim"]))
        return img

    def X(v):
        return ox + v * k

    def Y(v):
        return oy + (STATUS_H + v) * k

    for o in sorted(b.objs.values(), key=lambda o: o["order"]):
        if o["hidden"] or o["kind"] == "pix":
            continue
        kind = o["kind"]
        if kind == "rect":
            box = [X(o["x"]), Y(o["y"]), X(o["x"] + o["w"]) - 1, Y(o["y"] + o["h"]) - 1]
            if box[2] >= box[0] and box[3] >= box[1]:
                r = min(o["radius"], o["w"] // 2, o["h"] // 2) * k
                d.rounded_rectangle(box, radius=r, fill=rgb(o["color"]),
                                    outline=rgb(o["border"]) if o["border"] is not None else None, width=k)
        elif kind == "bar":
            box = [X(o["x"]), Y(o["y"]), X(o["x"] + o["w"]) - 1, Y(o["y"] + o["h"]) - 1]
            d.rounded_rectangle(box, radius=o["h"] * k // 2, fill=(0x1C, 0x1B, 0x26))
            fw = o["w"] * o["value"] // 100
            if fw > 0:
                d.rounded_rectangle([box[0], box[1], X(o["x"] + fw) - 1, box[3]], radius=o["h"] * k // 2,
                                    fill=rgb(THEME["green"]))
        elif kind == "label":
            f = font(o["size"], o["mono"], k)
            lines = wrap(d, o["text"], f, o["w"] * k) if o["w"] else o["text"].split("\n")
            lh = {12: 15, 14: 18, 20: 25, 32: 38}[o["size"]] * k
            for n, line in enumerate(lines):
                tx = X(o["x"])
                if o["w"] and o["align"] in ("center", "right"):
                    tw = d.textlength(line, font=f)
                    tx += (o["w"] * k - tw) / (2 if o["align"] == "center" else 1)
                d.text((tx, Y(o["y"]) + n * lh), line, font=f, fill=rgb(o["color"]))
        elif kind in ("image", "canvas"):
            draw_pix(img, o["pix"], o["x"], o["y"], o["scale"], k)
        elif kind == "list":
            vis = max(1, o["h"] // ROW_H)
            top_i = max(0, min(o["sel"] - vis + 1, len(o["items"]) - vis)) if o["sel"] >= vis else 0
            f14, fsub = font(14, False, k), font(12, True, k)
            if not o["items"]:
                d.text((X(o["x"] + 12), Y(o["y"] + 10)), "Nothing here", font=f14, fill=rgb(THEME["dim"]))
            for r in range(vis):
                i = top_i + r
                if i >= len(o["items"]):
                    break
                it = o["items"][i]
                title, sub, right = (it, "", "") if not isinstance(it, dict) else (
                    it.get("title", ""), it.get("sub", ""), it.get("right", ""))
                y0 = o["y"] + r * ROW_H
                if i == o["sel"]:
                    d.rectangle([X(o["x"]), Y(y0), X(o["x"] + o["w"]) - 1, Y(y0 + ROW_H) - 1], fill=(0x1C, 0x1B, 0x26))
                    d.rectangle([X(o["x"]), Y(y0), X(o["x"] + 3) - 1, Y(y0 + ROW_H) - 1], fill=rgb(THEME["green"]))
                d.text((X(o["x"] + 12), Y(y0 + (4 if sub else 10))), str(title), font=f14, fill=rgb(THEME["text"]))
                if sub:
                    d.text((X(o["x"] + 12), Y(y0 + 21)), str(sub), font=fsub, fill=rgb(THEME["dim"]))
                if right:
                    tw = d.textlength(str(right), font=fsub)
                    d.text((X(o["x"] + o["w"] - 10) - tw, Y(y0 + 12)), str(right), font=fsub, fill=rgb(THEME["dim"]))

    if b.hint:
        d.rectangle([X(0), Y(H - HINT_H), X(W) - 1, Y(H) - 1], fill=(17, 17, 22))
        d.text((X(6), Y(H - HINT_H + 2)), b.hint, font=f12, fill=rgb(THEME["dim"]))
    if b.modal:
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        od.rectangle([X(0), Y(0), X(W) - 1, Y(H) - 1], fill=(0, 0, 0, 178))
        img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"))
        d = ImageDraw.Draw(img)
        f14 = font(14, False, k)
        lines = wrap(d, b.modal["text"], f14, (W - 66) * k)
        ph = (len(lines) * 18 + 24 + 22) * k
        py = Y(0) + (H * k - ph) // 2
        d.rounded_rectangle([X(20), py, X(W - 20), py + ph], radius=6 * k, fill=rgb(THEME["panel"]),
                            outline=rgb(THEME["purple"]), width=k)
        for n, line in enumerate(lines):
            d.text((X(32), py + (12 + n * 18) * k), line, font=f14, fill=rgb(THEME["text"]))
        tail = "SELECT yes   CANCEL no" if b.modal["kind"] == "confirm" else "(keyboard)"
        d.text((X(32), py + (12 + len(lines) * 18 + 10) * k), tail, font=f12, fill=rgb(THEME["dim"]))
    if b.toasts:
        text = b.toasts[-1][1]
        f14 = font(14, False, k)
        tw = d.textlength(text, font=f14)
        cx = X(W // 2)
        d.rounded_rectangle([cx - tw / 2 - 10 * k, Y(H - 52), cx + tw / 2 + 10 * k, Y(H - 26)], radius=8 * k,
                            fill=(0x1C, 0x1B, 0x26), outline=rgb(THEME["border"]), width=k)
        d.text((cx - tw / 2, Y(H - 47)), text, font=f14, fill=rgb(THEME["text"]))
    return img


def side_by_side(images, gap=16):
    w = sum(i.width for i in images) + gap * (len(images) - 1)
    out = Image.new("RGB", (w, max(i.height for i in images)), (24, 24, 30))
    x = 0
    for i in images:
        out.paste(i, (x, 0))
        x += i.width + gap
    return out
