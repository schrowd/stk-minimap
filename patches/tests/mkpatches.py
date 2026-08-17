#!/usr/bin/env python3
"""
Regenerate the 4-patch series in patches/ from a working stk-code dev tree.

The tree holds all four patches at once, and four files carry hunks that
belong to different patches.  So rather than diffing per file, this replays
the series into a scratch worktree one commit at a time and diffs each step.

Usage: mkpatches.py <scratch-worktree-dir>

The scratch worktree should be a clean `git worktree add --detach` of the
dev tree's own HEAD (a pristine tag-1.5 checkout with no patches applied) -
this script writes patched file contents into it and commits each step, so
running it against anything else will produce nonsense.

STK_DEV_TREE overrides which tree to diff against (default: ~/stk-code, this
project's historical location - it is a from-scratch checkout with the
patches applied as *uncommitted* working-tree edits, not the patches/*.patch
files themselves, which is what makes it the source of truth to diff from).
"""
import os, pathlib, subprocess, sys

STK = os.environ.get("STK_DEV_TREE", os.path.expanduser("~/stk-code"))
OUT = str(pathlib.Path(__file__).resolve().parents[1])   # patches/
W   = sys.argv[1]

def sh(*a, cwd=W, **kw):
    return subprocess.run(a, cwd=cwd, check=True, text=True,
                          capture_output=True, **kw).stdout

def cur(p):
    with open(os.path.join(STK, p)) as f: return f.read()

def put(p, text):
    dst = os.path.join(W, p)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w") as f: f.write(text)

def drop(text, snippet, path):
    if snippet not in text:
        sys.exit("!! snippet not found in %s:\n%s" % (path, snippet[:200]))
    return text.replace(snippet, "", 1)

# ---------------------------------------------------------------- 0004-only
# The parts of the straddling files that belong to the sync server, held back
# until patch 4.  m_replay_name is introduced by 0004, so anything touching
# it has to wait - including ReplayControl::reset() clearing it, or the
# 0001-0003 intermediate state wouldn't compile.
RC_H = "src/replay/replay_control.hpp"
rc_h_final = cur(RC_H)
rc_h_0002 = rc_h_final
rc_h_0002 = drop(rc_h_0002, "#include <string>\n", RC_H)
rc_h_0002 = drop(rc_h_0002, """    /** File name of the loaded replay, for reporting to a client. */
    std::string m_replay_name;

""", RC_H)
rc_h_0002 = drop(rc_h_0002, """    // ------------------------------------------------------------------------
    std::string getReplayName() const
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        return m_replay_name;
    }
    // ------------------------------------------------------------------------
    void   setReplayName(const std::string &name)
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_replay_name = name;
        m_dirty       = true;
    }
""", RC_H)

RC_C = "src/replay/replay_control.cpp"
rc_c_final = cur(RC_C)
rc_c_0002 = drop(rc_c_final, """    // Cleared, not kept: World::reset() runs for every new world, so holding
    // on to the old name would report a replay as still loaded after leaving
    // it for an ordinary race.  When a replay *is* being watched the caller
    // sets it again immediately after this returns.
    m_replay_name.clear();
""", RC_C)

W_CPP = "src/modes/world.cpp"
w_final = cur(W_CPP)
w_0002 = drop(w_final, """            ReplayControl::get()->setReplayName(
                ReplayPlay::get()->getReplayFilename());
""", W_CPP)

M_CPP = "src/main.cpp"
m_final = cur(M_CPP)
m_0002 = m_final
m_0002 = drop(m_0002, '#include "replay/replay_sync_server.hpp"\n', M_CPP)
m_0002 = drop(m_0002, """    "       --sync-port=n      Let an external viewer drive replay playback over\\n"
    "                          127.0.0.1:n. Implies --replay-control.\\n"
""", M_CPP)
m_0002 = drop(m_0002, """
    if(CommandLine::has("--sync-port", &n))
    {
        // Opens a loopback control channel so an external viewer can drive
        // and follow replay playback.  Implies --replay-control: a viewer
        // that can't move the playback head is not much use.
        if (n <= 0 || n > 65535)
        {
            Log::error("ReplaySync", "--sync-port=%d is not a port number.", n);
        }
        else if (ReplayControl::get())
        {
            ReplayControl::get()->setEnabled(true);
            ReplaySyncServer::create((uint16_t)n);
        }
    }   // --sync-port
""", M_CPP)
m_0002 = drop(m_0002, """    // Before ReplayControl: the server thread reads it every cycle.
    ReplaySyncServer::destroy();
""", M_CPP)

# ------------------------------------------------------------------- series
SERIES = [
    ("0001-ghost-controller-seekable.patch", {
        "src/karts/controller/ghost_controller.cpp":
            cur("src/karts/controller/ghost_controller.cpp"),
        "src/karts/ghost_kart.cpp": cur("src/karts/ghost_kart.cpp"),
    }),
    ("0002-replay-playback-control.patch", {
        "src/karts/controller/ghost_controller.hpp":
            cur("src/karts/controller/ghost_controller.hpp"),
        RC_H: rc_h_0002,
        RC_C: rc_c_0002,
        "src/modes/world_status.cpp": cur("src/modes/world_status.cpp"),
        W_CPP: w_0002,
        M_CPP: m_0002,
    }),
    ("0003-fix-replay-trailing-blank-line-crash.patch", {
        "src/replay/replay_play.cpp": cur("src/replay/replay_play.cpp"),
    }),
    ("0004-replay-sync-server.patch", {
        "src/replay/replay_sync_server.hpp":
            cur("src/replay/replay_sync_server.hpp"),
        "src/replay/replay_sync_server.cpp":
            cur("src/replay/replay_sync_server.cpp"),
        RC_H: rc_h_final,
        RC_C: rc_c_final,
        W_CPP: w_final,
        M_CPP: m_final,
    }),
]

for name, files in SERIES:
    for p, text in files.items():
        put(p, text)
    sh("git", "add", "-A")
    diff = sh("git", "diff", "--cached", "--binary")
    with open(os.path.join(OUT, name), "w") as f:
        f.write(diff)
    sh("git", "-c", "user.name=x", "-c", "user.email=x@x",
       "commit", "-q", "-m", name)
    print("%-52s %5d lines" % (name, diff.count("\n")))
