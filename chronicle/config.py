import os, json, pathlib

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get("CHRONICLE_ROOT", HOME / ".chronicle"))
DB_PATH = ROOT / "chronicle.db"
SOURCE = pathlib.Path(os.environ.get("CHRONICLE_SOURCE", HOME / ".claude" / "projects"))
# where Chronicle itself is installed — derived from this file, never assumed,
# so a clone works from any directory on any machine
INSTALL_DIR = pathlib.Path(__file__).resolve().parent.parent
MEMORY_DIR = pathlib.Path(os.environ.get("CHRONICLE_MEMORY", INSTALL_DIR))
ALIASES_PATH = MEMORY_DIR / "aliases.json"
EXAMPLE_ALIASES = INSTALL_DIR / "aliases.example.json"

# an episode ends after this much idle time between prompts
GAP_SECONDS = int(os.environ.get("CHRONICLE_GAP", 4 * 3600))
# per-message text caps, keeps the index small enough to never blow a context window
MAX_USER_TEXT = 6000
MAX_ASST_TEXT = 4000
MAX_BODY = 60000

ROOT.mkdir(parents=True, exist_ok=True)
try:
    # the index is a verbatim record of every prompt typed on this machine —
    # it has no business being readable by other accounts on a shared box
    ROOT.chmod(0o700)
except OSError:
    pass


DEFAULT_IGNORE = ["/private/tmp", "/tmp", "/var/folders",
                  str(HOME / ".claude"), str(HOME / ".chronicle")]


def load_ignore_roots():
    if ALIASES_PATH.exists():
        try:
            d = json.loads(ALIASES_PATH.read_text())
            return d.get("ignore_roots", DEFAULT_IGNORE)
        except Exception:
            pass
    return DEFAULT_IGNORE


def load_aliases():
    """path-prefix -> logical project name. Longest prefix wins.

    A fresh install has no aliases.json; that is fine. Projects then resolve from
    directory names until the user maps them."""
    if ALIASES_PATH.exists():
        try:
            d = json.loads(ALIASES_PATH.read_text())
            return d.get("projects", {}), d.get("labels", {})
        except Exception:
            pass
    return {}, {}


def invocation():
    """How to tell the user to run us: `chronicle` when it is on PATH and points
    here, otherwise the absolute launcher. Printing `./chronicle-cli` is wrong
    everywhere except the install directory."""
    import shutil
    found = shutil.which("chronicle")
    launcher = INSTALL_DIR / "chronicle-cli"
    try:
        if found and os.path.realpath(found) == os.path.realpath(launcher):
            return "chronicle"
    except OSError:
        pass
    return str(launcher)
