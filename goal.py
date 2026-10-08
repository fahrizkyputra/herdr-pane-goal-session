#!/usr/bin/env python3
"""pane-goal-session: persistent per-pane goal + agent session ID for Herdr.

State: $HERDR_PLUGIN_STATE_DIR/goals-<session>.json
Sidebar tokens: $goal (free text), $session (short agent session id).

Subcommands:
  open-editor              action: open the popup editor for the focused pane
  editor                   popup entrypoint
  set <text>               set goal            (target: $HERDR_PANE_ID or --pane ID)
  clear                    clear goal
  get                      print goal
  session [<id>|--auto]    print, set (locks) or unlock+resync session
  resume                   print resume command
  list                     print all panes: goal, session, resume command
  sync                     record current agent sessions for all panes
  restore                  re-publish all saved state (startup hook)
  event                    handle pane.* event hooks
"""

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

GOAL_TOKEN = "goal"
SESSION_TOKEN = "session"
SOURCE = "pane-goal-session"
LEGACY_PLUGIN_ID = "pane-goal"
MAX_LEN = 80  # Herdr caps token values at 80 chars
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "pane-goal-session")

# agent -> resume argv prefix; the session value is appended.
# Source: https://herdr.dev/docs/session-state/ (native agent session restore)
RESUME = {
    "pi": ["pi", "--session"],
    "antigravity-cli": ["agy", "--conversation"],
    "omp": ["omp", "--resume="],
    "claude": ["claude", "--resume"],
    "codex": ["codex", "resume"],
    "cursor": ["cursor-agent", "--resume"],
    "grok": ["grok", "--resume"],
    "copilot": ["copilot", "--resume="],
    "devin": ["devin", "--resume"],
    "droid": ["droid", "--resume"],
    "kimi": ["kimi", "--session"],
    "qodercli": ["qodercli", "--resume"],
    "qwen": ["qwen", "--resume"],
    "letta": ["letta", "--conversation"],
    "opencode": ["opencode", "--session"],
    "kilo": ["kilo", "--session"],
    "hermes": ["hermes", "--resume"],
    "mastracode": ["mastracode", "--thread"],
}
AGENT_ALIASES = {"antigravity": "antigravity-cli", "agy": "antigravity-cli",
                 "cursor-agent": "cursor", "qwen-code": "qwen",
                 "letta-code": "letta", "qoder": "qodercli"}

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# ---------- herdr helpers ----------

def herdr_bin():
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr(*args, check=True):
    proc = subprocess.run(
        [herdr_bin(), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            "herdr %s failed (%s): %s"
            % (" ".join(args), proc.returncode, proc.stderr.strip())
        )
    return proc


def herdr_json(*args):
    out = herdr(*args).stdout
    return json.loads(out) if out.strip() else {}


def pane_info(pane_id):
    try:
        return herdr_json("pane", "get", pane_id).get("result", {}).get("pane", {})
    except Exception:
        return {}


def live_panes():
    """Return {pane_id: pane_info} for every pane in the running server."""
    panes = {}
    ws = herdr_json("workspace", "list")
    for w in ws.get("result", {}).get("workspaces", []):
        wid = w.get("workspace_id") or w.get("id")
        if not wid:
            continue
        res = herdr_json("pane", "list", "--workspace", wid)
        for p in res.get("result", {}).get("panes", []):
            panes[p["pane_id"]] = p
    return panes


# ---------- session helpers ----------

def norm_agent(agent):
    agent = (agent or "").strip().lower()
    return AGENT_ALIASES.get(agent, agent)


def short_id(value):
    """8-char id for the sidebar: UUID prefix if present, else basename prefix."""
    if not value:
        return ""
    m = UUID_RE.search(value)
    if m:
        return m.group(0)[:8]
    base = os.path.basename(value.rstrip("/"))
    base = re.sub(r"\.jsonl?$", "", base)
    return base[:8]


def session_from_pane(info):
    """Session dict from Herdr's agent_session, or None."""
    s = info.get("agent_session") or {}
    value = (s.get("value") or "").strip()
    if not value:
        return None
    return {
        "agent": norm_agent(s.get("agent") or info.get("agent")),
        "value": value,
        "kind": s.get("kind") or "",
        "cwd": info.get("foreground_cwd") or info.get("cwd") or "",
    }


def resume_command(entry):
    sess = (entry or {}).get("session") or {}
    value = sess.get("value")
    if not value:
        return ""
    agent = norm_agent(sess.get("agent"))
    prefix = RESUME.get(agent)
    if not prefix:
        return ""
    if prefix[-1].endswith("="):
        argv = prefix[:-1] + [prefix[-1] + value]
    else:
        argv = prefix + [value]
    cmd = " ".join(shlex.quote(a) for a in argv)
    cwd = sess.get("cwd") or entry.get("cwd")
    if cwd:
        cmd = "cd %s && %s" % (shlex.quote(cwd), cmd)
    return cmd


def session_label(entry):
    sess = (entry or {}).get("session") or {}
    if not sess.get("value"):
        return ""
    agent = sess.get("agent") or "?"
    return "%s:%s" % (agent, short_id(sess["value"]))


# ---------- state ----------

def session_name():
    """Herdr session this command talks to. Pane IDs are only unique per
    session, so state is kept per session."""
    sock = os.environ.get("HERDR_SOCKET_PATH") or ""
    parent = os.path.dirname(sock)
    if os.path.basename(os.path.dirname(parent)) == "sessions":
        name = os.path.basename(parent)
    else:
        name = "default"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "default"


def state_dir():
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
        "~/.local/state/herdr-pane-goal-session")
    os.makedirs(base, exist_ok=True)
    return base


def migrate_legacy(base, path, name):
    """One-time copy from the old `pane-goal` plugin state dir."""
    if os.path.exists(path):
        return
    legacy_dir = os.path.join(os.path.dirname(base.rstrip("/")), LEGACY_PLUGIN_ID)
    for candidate in ("goals-%s.json" % name,
                      "goals.json" if name == "default" else None):
        if not candidate:
            continue
        src = os.path.join(legacy_dir, candidate)
        if os.path.isfile(src):
            shutil.copyfile(src, path)
            return


def state_path():
    base = state_dir()
    name = session_name()
    path = os.path.join(base, "goals-%s.json" % name)
    legacy = os.path.join(base, "goals.json")
    if name == "default" and not os.path.exists(path) and os.path.exists(legacy):
        os.replace(legacy, path)
    migrate_legacy(base, path, name)
    return path


@contextlib.contextmanager
def locked_state():
    """Yield the mutable entries dict under an exclusive lock; save on exit."""
    path = state_path()
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            data = {}
        entries = data.get("goals", {}) if isinstance(data, dict) else {}
        before = json.dumps(entries, sort_keys=True)
        yield entries
        for pane in [p for p, e in entries.items() if is_empty(e)]:
            del entries[pane]
        if json.dumps(entries, sort_keys=True) != before:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 2, "goals": entries}, f, indent=2,
                          ensure_ascii=False)
            os.replace(tmp, path)


def is_empty(entry):
    return not (entry or {}).get("goal") and not ((entry or {}).get("session") or {}).get("value")


def read_entries():
    with locked_state() as entries:
        return json.loads(json.dumps(entries))


def normalize(text):
    return " ".join((text or "").split())[:MAX_LEN]


# ---------- publish ----------

def publish(pane_id, entry):
    args = ["pane", "report-metadata", pane_id, "--source", SOURCE]
    goal = (entry or {}).get("goal") or ""
    label = session_label(entry)
    args += ["--token", "%s=%s" % (GOAL_TOKEN, goal)] if goal else ["--clear-token", GOAL_TOKEN]
    args += ["--token", "%s=%s" % (SESSION_TOKEN, label)] if label else ["--clear-token", SESSION_TOKEN]
    herdr(*args, check=False)


# ---------- mutations ----------

def set_goal(pane, text, cwd=None):
    text = normalize(text)
    with locked_state() as entries:
        entry = entries.setdefault(pane, {})
        entry["goal"] = text
        if cwd:
            entry["cwd"] = cwd
        entry["updated"] = int(time.time())
        snapshot = dict(entry)
    publish(pane, snapshot)
    return text


def set_session_manual(pane, value, cwd=None, agent=None):
    """Lock the pane's session to a user-provided value."""
    value = value.strip()
    with locked_state() as entries:
        entry = entries.setdefault(pane, {})
        old = entry.get("session") or {}
        entry["session"] = {
            "agent": norm_agent(agent or old.get("agent")),
            "value": value,
            "kind": "manual",
            "cwd": old.get("cwd") or cwd or entry.get("cwd") or "",
            "locked": True,
            "updated": int(time.time()),
        }
        snapshot = dict(entry)
    publish(pane, snapshot)
    return snapshot


def sync_pane(pane, info=None, unlock=False):
    """Record the pane's current agent session unless locked. Empty never
    overwrites. Returns the entry snapshot (or None if nothing changed)."""
    info = info if info is not None else pane_info(pane)
    current = session_from_pane(info)
    with locked_state() as entries:
        entry = entries.get(pane, {})
        sess = dict(entry.get("session") or {})
        changed = False

        if unlock and sess.get("locked"):
            sess.pop("locked", None)
            changed = True
        elif sess.get("locked"):
            return None  # manual value wins until the user unlocks it

        if current and (sess.get("value"), sess.get("agent")) != (
                current["value"], current["agent"]):
            current["updated"] = int(time.time())
            sess = current
            changed = True
        # no current session: keep the old one (empty never overwrites)

        if not changed:
            return None
        entry["session"] = sess
        entries[pane] = entry
        return dict(entry)


# ---------- target pane resolution ----------

def context():
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        return {}


def target_pane(argv):
    if "--pane" in argv:
        i = argv.index("--pane")
        pane = argv[i + 1]
        del argv[i:i + 2]
        return pane
    return (
        os.environ.get("PANE_GOAL_TARGET")
        or context().get("focused_pane_id")
        or os.environ.get("HERDR_PANE_ID")
    )


# ---------- clipboard ----------

def copy_to_clipboard(text):
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                 ["xsel", "--clipboard", "--input"]):
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text, universal_newlines=True,
                               check=True, timeout=3)
                return argv[0]
            except Exception:
                continue
    try:  # OSC 52 fallback through the popup terminal
        import base64
        sys.stdout.write("\033]52;c;%s\a" % base64.b64encode(text.encode()).decode())
        sys.stdout.flush()
        return "osc52"
    except Exception:
        return None


# ---------- commands ----------

def cmd_open_editor(argv):
    pane = target_pane(argv)
    if not pane:
        print("pane-goal-session: no focused pane", file=sys.stderr)
        return 1
    herdr("plugin", "pane", "open", "--plugin", PLUGIN_ID,
          "--entrypoint", "editor", "--placement", "popup",
          "--env", "PANE_GOAL_TARGET=%s" % pane)
    return 0


def prompt(label, prefill):
    try:
        import readline
        readline.set_startup_hook(lambda: readline.insert_text(prefill or ""))
    except Exception:
        readline = None
    try:
        return input(label)
    finally:
        if readline:
            readline.set_startup_hook(None)


def cmd_editor(argv):
    pane = target_pane(argv)
    if not pane:
        print("No target pane. Press Enter to close.")
        input()
        return 1

    info = pane_info(pane)
    snap = sync_pane(pane, info)
    if snap:
        publish(pane, snap)
    entry = read_entries().get(pane, {})
    sess = entry.get("session") or {}
    label = info.get("label") or info.get("agent") or ""
    cwd = info.get("foreground_cwd") or info.get("cwd") or ""

    print("\033[1mPane goal & session\033[0m  %s%s" % (pane, ("  · " + label) if label else ""))
    if cwd:
        print("\033[2m%s\033[0m" % cwd)
    lock_note = " (locked, empty = auto)" if sess.get("locked") else " (auto)"
    print("\033[2mEnter: next/save · empty goal: clear · Ctrl+C: cancel\033[0m")
    print()

    try:
        goal = prompt("Goal    › ", entry.get("goal", ""))
        print("\033[2mSession%s\033[0m" % (lock_note if sess.get("value") else " (no agent session detected)"))
        new_value = prompt("Session › ", sess.get("value", "")).strip()
    except (KeyboardInterrupt, EOFError):
        return 0

    set_goal(pane, goal, cwd or None)
    if not new_value:
        snap = sync_pane(pane, info, unlock=True)
        if snap:
            publish(pane, snap)
    elif new_value != sess.get("value"):
        set_session_manual(pane, new_value, cwd, agent=info.get("agent"))

    entry = read_entries().get(pane, {})
    print()
    cmd = resume_command(entry)
    sess = entry.get("session") or {}
    if cmd:
        how = copy_to_clipboard(cmd)
        print("\033[1mResume:\033[0m %s" % cmd)
        print("\033[2m%s\033[0m" % ("Copied to clipboard." if how else "Copy it manually."))
    elif sess.get("value"):
        print("\033[1m%s\033[0m  session: %s" % (sess.get("agent") or "agent", sess["value"]))
        print("\033[2mNo known resume command for this agent.\033[0m")
    else:
        print("Saved." if entry.get("goal") else "Cleared.")
    print("\033[2mPress Enter to close.\033[0m")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    return 0


def cmd_set(argv):
    pane = target_pane(argv)
    if not pane:
        print("pane-goal-session: run inside Herdr or pass --pane ID", file=sys.stderr)
        return 1
    saved = set_goal(pane, " ".join(argv), os.getcwd())
    print("%s: %s" % (pane, saved or "(cleared)"))
    return 0


def cmd_clear(argv):
    pane_args = argv[argv.index("--pane"):argv.index("--pane") + 2] if "--pane" in argv else []
    return cmd_set(pane_args)


def cmd_get(argv):
    pane = target_pane(argv)
    goal = read_entries().get(pane or "", {}).get("goal")
    if goal:
        print(goal)
    return 0 if goal else 1


def cmd_session(argv):
    pane = target_pane(argv)
    if not pane:
        print("pane-goal-session: run inside Herdr or pass --pane ID", file=sys.stderr)
        return 1
    if argv == ["--auto"]:
        snap = sync_pane(pane, unlock=True)
        if snap:
            publish(pane, snap)
    elif argv:
        set_session_manual(pane, " ".join(argv), os.getcwd(),
                           agent=pane_info(pane).get("agent"))
    sess = read_entries().get(pane, {}).get("session") or {}
    if not sess.get("value"):
        return 1
    print("%s %s%s" % (sess.get("agent") or "?", sess["value"],
                       "  [locked]" if sess.get("locked") else ""))
    return 0


def cmd_resume(argv):
    pane = target_pane(argv)
    cmd = resume_command(read_entries().get(pane or "", {}))
    if cmd:
        print(cmd)
    return 0 if cmd else 1


def cmd_list(_argv):
    for pane, entry in sorted(read_entries().items()):
        sess = entry.get("session") or {}
        print("%s  %s" % (pane, entry.get("goal") or "-"))
        if sess.get("value"):
            print("    session: %s %s%s" % (sess.get("agent") or "?", sess["value"],
                                          "  [locked]" if sess.get("locked") else ""))
            cmd = resume_command(entry)
            if cmd:
                print("    resume:  %s" % cmd)
    return 0


def cmd_sync(_argv):
    panes = live_panes()
    n = 0
    for pane, info in panes.items():
        snap = sync_pane(pane, info)
        if snap:
            publish(pane, snap)
            n += 1
    print("pane-goal-session: synced %d" % n)
    return 0


def cmd_restore(_argv):
    try:
        panes = live_panes()
    except Exception as e:
        print("pane-goal-session restore: %s" % e, file=sys.stderr)
        return 1
    pruned = 0
    with locked_state() as entries:
        for pane in list(entries):
            if pane not in panes and panes:  # only prune with a real pane list
                del entries[pane]
                pruned += 1
    # record sessions for agents that resumed natively, then publish all
    for pane, info in panes.items():
        sync_pane(pane, info)
    published = 0
    for pane, entry in read_entries().items():
        if pane in panes:
            publish(pane, entry)
            published += 1
    print("pane-goal-session: restored %d, pruned %d" % (published, pruned))
    return 0


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for v in obj.values():
            found = find_key(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_key(v, key)
            if found:
                return found
    return None


def cmd_event(_argv):
    name = os.environ.get("HERDR_PLUGIN_EVENT", "")
    try:
        payload = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except ValueError:
        payload = {}

    if name == "pane.closed":
        pane = find_key(payload, "pane_id")
        if pane:
            with locked_state() as entries:
                entries.pop(pane, None)
    elif name == "pane.moved":
        old = find_key(payload, "previous_pane_id")
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        new = ((data or {}).get("pane") or {}).get("pane_id")
        if old and new and old != new:
            with locked_state() as entries:
                entry = entries.pop(old, None)
                if entry:
                    entries[new] = entry
            if entry:
                publish(new, entry)
    elif name in ("pane.agent_detected", "pane.agent_status_changed"):
        pane = find_key(payload, "pane_id")
        if pane:
            snap = sync_pane(pane)
            if snap:
                publish(pane, snap)
    return 0


COMMANDS = {
    "open-editor": cmd_open_editor,
    "editor": cmd_editor,
    "set": cmd_set,
    "clear": cmd_clear,
    "get": cmd_get,
    "session": cmd_session,
    "resume": cmd_resume,
    "list": cmd_list,
    "sync": cmd_sync,
    "restore": cmd_restore,
    "event": cmd_event,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
