"""library-utils.js: 공용 라이브러리 화면의 순수 함수 (node 없으면 skip)."""
import json
import shutil
import subprocess

import pytest

from scripts.ui.server import STATIC_DIR

NODE = shutil.which("node")
UTILS = STATIC_DIR / "library-utils.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run_js(expr: str):
    code = (
        f"import({json.dumps(UTILS.as_uri())}).then(m => "
        f"console.log(JSON.stringify((function(m){{ return {expr}; }})(m))))"
    )
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_imports():
    assert "import " not in UTILS.read_text(encoding="utf-8").replace("import(", "")


@pytest.mark.parametrize("remote, name", [
    ("git@github.com:me/friends-lib.git", "friends-lib"),
    ("https://github.com/me/lib", "lib"),
    ("https://github.com/me/lib/", "lib"),
    (r"C:\Users\a\bare.git", "bare"),
    (r"\\nas\share\team-lib" + "\\", "team-lib"),
    ("file:///C:/r/team.git", "team"),
    ("(로컬) t7lib.git", "(로컬) t7lib"),         # the server masks local folders this way
    ("", "연결됨"),
    (None, "연결됨"),
])
def test_repo_name_never_shows_a_path(remote, name):
    assert run_js(f"m.repoName({json.dumps(remote)})") == name
