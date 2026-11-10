"""Builds the playable browser preview: one HTML page with the app's files inside.

    python3 tools/web/build.py <out.html> [--fengari <script url>]

The page runs app/'s Lua on Fengari (Lua 5.3 written in JavaScript) with a
stand-in for the badge API and screen (template.html), and shows two badges
that can swap with each other. It is for trying the game out, not for
checking it against the badge: memory limits aren't enforced and the drawing
is approximate. tools/sim/ is the stricter test.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(os.path.dirname(HERE)), "app")
FENGARI = "https://cdn.jsdelivr.net/npm/fengari-web@0.1.4/dist/fengari-web.js"

args = sys.argv[1:]
if "--fengari" in args:
    i = args.index("--fengari")
    FENGARI = args[i + 1]
    del args[i:i + 2]
if len(args) != 1:
    sys.exit(__doc__)

files = {}
for root, _, names in os.walk(APP):
    for name in sorted(names):
        if name.endswith((".lua", ".txt")):
            path = os.path.join(root, name)
            files[os.path.relpath(path, APP).replace(os.sep, "/")] = open(path, encoding="utf-8").read()

page = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
page = page.replace("__FILES__", json.dumps(files).replace("</", "<\\/"))
page = page.replace("__FENGARI__", FENGARI)
with open(args[0], "w", encoding="utf-8") as f:
    f.write(page)
print("%s: %d files, %.0f KB" % (args[0], len(files), len(page) / 1024))
