"""scripts/ui/static/diagnose-gaps.js is pure, so the gap rules run under node without a DOM."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = """
import {recordMismatch, previewStale} from %s;
const c = JSON.parse(process.env.CASES);
console.log(JSON.stringify(c.map(([fn, ...a]) => (fn === "m" ? recordMismatch : previewStale)(...a))));
""" % json.dumps((STATIC_DIR / "diagnose-gaps.js").as_uri())


def run(cases):
    import os
    done = subprocess.run([NODE, "--input-type=module", "-e", SCRIPT], env=dict(os.environ, CASES=json.dumps(cases)),
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_record_mismatch_treats_intentional_gaps_as_expected():
    got = run([
        ["m", ["a", "b"], [], ["a", "b"]],          # unchanged
        ["m", ["a", "b"], ["a"], ["b"]],            # toggled a on: it left the compare -> expected
        ["m", ["a", "b"], [], ["a", "b"]],          # untoggled: restored
        ["m", ["a", "b"], [], ["a", "b", "c"]],     # new gap
        ["m", ["a", "b"], ["a"], ["b", "c"]],       # toggled + new gap
        ["m", ["b"], [], ["a", "b"]],               # an intentional-at-save gap came back
    ])
    assert got == [False, False, False, True, True, True]


def test_preview_stale_only_for_gaps_the_notes_do_not_cover():
    got = run([
        ["s", ["a", "b"], ["a", "b"]],
        ["s", ["a", "b"], ["b"]],                   # toggled a on: removed gap is fine
        ["s", ["a", "b"], ["a", "b"]],              # untoggled: back
        ["s", ["a", "b"], ["a", "b", "c"]],         # new gap
        ["s", ["a", "b"], []],
    ])
    assert got == [False, False, False, True, False]
