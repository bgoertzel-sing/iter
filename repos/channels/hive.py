"""hive channel: a long-lived Iter agent's work, from a hive record (hive-runtime, adapters/iter).

The agent keeps running and keeps its memory; its tasks come from the record, never from chat.
Install: copy this file to the agent's channels/ and tools/hive_task.py to its tools/, then write
.hive/config.json in the agent's folder (README.md beside this file).

- receive(): what the record says this agent should now do, as plain text: a task assigned to it
  (with its order), an answer to its question, a review of its result, a result for it to review,
  and the outcome of each action it asked for.
- send(content): a short progress note on the agent's one open task (a task.reported progress).
- Actions (hive_task tool) and notes are queued as files; a small daemon does the slow parts (record
  reads, git commits and pushes, pins, signed events), so receive() and the tool return within Iter's
  5-second limit. The daemon is started by the channel on first use, or, in *sidecar* mode, runs
  elsewhere (e.g. on the host, outside the agent's container, as the agent's uid: sidecar.py), with
  the agent's record key where the agent itself can't read it. In sidecar mode the agent's
  .hive/config.json holds only {"daemon": "sidecar"}, and the sidecar gets the real config.
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

MAX_TEXT = 6000                                      # of an order, answer or review, per message


def configure(root, conf=None):
    """Where the agent's folder and the config are. The channel uses the defaults (this file's agent);
    a sidecar names them."""
    global ROOT, CONF, RUN, INBOX, OUTBOX, PID, SEEN, TASKS, LOG
    ROOT = Path(root).resolve()                      # the agent's Iter folder
    CONF = Path(conf) if conf else ROOT / ".hive" / "config.json"
    RUN = ROOT / ".runtime" / "hive"
    INBOX, OUTBOX, PID = RUN / "inbox", RUN / "outbox", RUN / "pid"
    SEEN, TASKS, LOG = RUN / "seen.json", RUN / "tasks", RUN / "log.txt"


configure(Path(__file__).resolve().parent.parent)


def _dirs():
    for d in (INBOX, OUTBOX, TASKS):
        d.mkdir(parents=True, exist_ok=True)


def _put(folder, payload):
    """Write one queue file atomically: readers never see half a file."""
    _dirs()
    name = f"{time.time_ns()}-{os.getpid()}"
    tmp = folder / f".{name}.tmp"
    tmp.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    tmp.replace(folder / name)


def ensure_daemon():
    if not CONF.is_file():
        return False
    _dirs()
    try:
        if json.loads(CONF.read_text()).get("daemon") == "sidecar":
            return True                              # runs elsewhere; never spawned from here
    except ValueError:
        return False
    try:
        os.kill(int(PID.read_text()), 0)
        return True
    except Exception:
        pass
    p = subprocess.Popen([sys.executable, __file__, "--daemon"], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    PID.write_text(str(p.pid))
    return True


def receive_now():
    """The queued messages, without starting anything (hive-task inbox)."""
    out = []
    for path in sorted(INBOX.glob("[0-9]*")):
        try:
            out.append(path.read_text())
            path.unlink()
        except FileNotFoundError:
            pass
    return "\n\n".join(out)


def receive():
    if not ensure_daemon():
        return ""
    out = []
    for path in sorted(INBOX.glob("[0-9]*")):
        try:
            out.append(path.read_text())
            path.unlink()
        except FileNotFoundError:
            pass
    return "\n\n".join(out)


def send(content):
    if not ensure_daemon():
        raise RuntimeError(f"hive channel not configured: no {CONF}")
    _put(OUTBOX, {"action": "progress", "task": "", "text": str(content), "via": "send"})


# ---------------------------------------------------------------- daemon ------------------------

class Failed(Exception):
    pass


def log(msg):
    with LOG.open("a") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}\n")


DELIVER = None   # a command (list) run per message with {text} filled in, instead of the inbox file


def tell(text):
    if DELIVER:
        cmd = [text if a == "{text}" else a for a in DELIVER]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        log(f"delivered ({r.returncode}): {text[:60]!r}")
        if r.returncode:
            _put(INBOX, text)                        # keep it for the next inbox read
        return
    _put(INBOX, text)


class Hive:
    """The record and the agent's clones, as config.json describes them."""

    def __init__(self, conf):
        self.c = conf
        self.actor = conf["actor"]
        self.ws = conf["workspace"]          # {"repository": ..., "clone": ..., "branch": ...}
        # code repository name -> the agent's clone: config's "repos", then those found earlier (find_clone)
        found = json.loads((RUN / "repos.json").read_text()) if (RUN / "repos.json").is_file() else {}
        self.repos = {**found, **conf.get("repos", {})}
        self.reg = json.loads(Path(conf["registry"]).read_text())
        for name in [self.ws["repository"], *self.repos]:
            if name not in self.reg["repositories"]:
                raise Failed(f"{name} isn't in the registry {conf['registry']}")
        self.write_registry()
        self.env = {**os.environ, "HIVE_URL": conf["hive_url"], "HIVE_ID": conf["hive_id"],
                    "HIVE_KEY_FILE": conf["key_file"], "HIVE_VIA": f"iter:{self.actor}",
                    "HIVEPIN_REPOSITORY_REGISTRY_PATH": str(RUN / "registry.json"),
                    "GIT_OPTIONAL_LOCKS": "0"}   # only read the agent's clones: never refresh their index

    def write_registry(self):
        """Pins resolve commits in the registry's local_path: point it at this agent's own clones, as
        hive-runtime's launcher does for a task folder."""
        reg = json.loads(json.dumps(self.reg))
        for name, clone in [(self.ws["repository"], self.ws["clone"]), *self.repos.items()]:
            reg["repositories"][name]["local_path"] = clone
        (RUN / "registry.json").write_text(json.dumps(reg, indent=1))

    def clone(self, repo):
        """The agent's clone of a registered repository: from config's "repos", or else the git folder under
        config's "repo_search" folders (default: the agent's folder) whose remote is one of the repository's
        fetch URLs in the registry. A found clone is remembered (.runtime/hive/repos.json)."""
        if repo in self.repos:
            return self.repos[repo]
        spec = self.reg["repositories"].get(repo)
        if not spec:
            raise Failed(f"{repo} isn't in the hive's registry")
        want = {_norm_url(u) for u in spec.get("fetch_urls", [])}
        hits = []
        for top in self.c.get("repo_search") or [str(ROOT)]:
            for d, sub, files in os.walk(top):
                if d[len(top):].count(os.sep) >= 4:
                    sub[:] = []
                    continue
                sub[:] = [x for x in sub if x not in SKIP_DIRS]
                if ".git" in sub or ".git" in files:
                    urls = self.run("git", "-C", d, "remote", "-v", check=False).split()
                    if want & {_norm_url(u) for u in urls[1::3]}:
                        hits.append(d)
                    sub[:] = [x for x in sub if x != ".git"]
        if not hits:
            raise Failed(f"no clone of {repo} found (looked under {', '.join(self.c.get('repo_search') or [str(ROOT)])}): "
                         f"clone {spec['fetch_urls'][0]} there, or name it under \"repos\" in the sidecar's config")
        hits.sort(key=lambda d: (Path(d).name.lower() != repo, len(d)))   # a folder named like it, then the shallowest
        self.repos[repo] = hits[0]
        found = json.loads((RUN / "repos.json").read_text()) if (RUN / "repos.json").is_file() else {}
        (RUN / "repos.json").write_text(json.dumps({**found, repo: hits[0]}, indent=1))
        self.write_registry()
        return hits[0]

    def run(self, *cmd, cwd=None, check=True):
        r = subprocess.run(cmd, cwd=cwd, env=self.env, capture_output=True, text=True, timeout=300)
        if check and r.returncode != 0:
            raise Failed(f"{' '.join(cmd[:3])}: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout

    def hive(self, *args):
        return self.run(self.c.get("hive_bin", "hive"), *args)

    def git(self, clone, *args, check=True):
        return self.run("git", "-C", clone, *args, check=check)

    def state(self):
        return json.loads(self.hive("state"))

    def last_event(self, task, *types):
        """The id of the task's latest event of these types, as the cause of what follows (A5)."""
        try:
            evs = json.loads(self.hive("task", task)).get("events") or []
        except (Failed, ValueError):
            return None
        return next((e["event_id"] for e in reversed(evs) if e["type"] in types), None)

    def emit(self, etype, task, data=None, refs=(), cause=None):
        args = ["emit", etype, "--task", task]
        if cause:
            args += ["--cause-event", cause]
        if data is not None:
            args += ["--data", json.dumps(data)]
        for rel, pin in refs:
            args += ["--ref", f"{rel}={pin}"]
        out = self.hive(*args)
        try:
            return json.loads(out)
        except ValueError:
            raise Failed(out.strip()[-400:])

    def pin(self, repository, commit, path=None, name="pin"):
        f = RUN / "pins" / f"{name}-{time.time_ns()}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        args = [self.c.get("hive_pin_bin", "hive-pin"), "mint", repository] + ([path] if path else [])
        self.run(*args, "--commit", commit, "--output", str(f))
        return str(f)

    def read_pin(self, pin):
        """A pin's text from the agent's workspace clone, or a pointer to it."""
        if not pin:
            return ""
        repo, sha, path = pin.get("repository"), pin.get("commit_oid", "").split(":")[-1], pin.get("path")
        where = f"{repo}@{sha[:12]}" + (f":{path}" if path else "")
        if repo != self.ws["repository"] or not path:
            return f"({where})"
        clone = self.ws["clone"]
        self.git(clone, "fetch", "-q", "origin", check=False)
        text = self.git(clone, "show", f"{sha}:{path}", check=False)
        if not text:
            return f"({where}: not readable in {clone})"
        return f"({where})\n{text[:MAX_TEXT]}" + ("\n[... truncated]" if len(text) > MAX_TEXT else "")

    def order(self, pid, text):
        """Commit a proposed task's order to the workspace (proposals/), push it, and return its pin."""
        rel = Path("proposals") / f"{datetime.date.today().isoformat()}-{pid}.md"
        return self.commit_file(rel, text, f"{pid}: proposed", "order")

    def commit_file(self, rel, content, msg, name):
        clone, branch = self.ws["clone"], self.ws.get("branch", "main")
        self.git(clone, "fetch", "-q", "origin")
        self.git(clone, "checkout", "-q", branch)
        self.git(clone, "merge", "-q", "--ff-only", f"origin/{branch}")
        (Path(clone) / rel).parent.mkdir(parents=True, exist_ok=True)
        (Path(clone) / rel).write_text(content)
        self.git(clone, "add", str(rel))
        self.git(clone, "-c", f"user.name={self.actor}", "-c", f"user.email={self.actor}@{self.c['hive_id']}.invalid",
                 "commit", "-q", "-m", msg)
        self.git(clone, "push", "-q", "origin", f"HEAD:{branch}")
        return self.pin(self.ws["repository"], self.git(clone, "rev-parse", "HEAD").strip(), str(rel), name)

    def report(self, task, kind, text):
        """Commit a report to the workspace, push it, and return its pin."""
        clone, branch = self.ws["clone"], self.ws.get("branch", "main")
        self.git(clone, "fetch", "-q", "origin")
        self.git(clone, "checkout", "-q", branch)
        self.git(clone, "merge", "-q", "--ff-only", f"origin/{branch}")
        project = task.get("project") or "tasks"
        day = datetime.date.today().isoformat()
        rel_dir = Path("projects") / project / "reports"
        n = 1
        while (Path(clone) / rel_dir / f"{day}-{task['id']}-{kind}-{n}.md").exists():
            n += 1
        rel = rel_dir / f"{day}-{task['id']}-{kind}-{n}.md"
        (Path(clone) / rel).parent.mkdir(parents=True, exist_ok=True)
        (Path(clone) / rel).write_text(f"# {task['id']}: {kind} {n}\n\n{self.actor}, {day}\n\n{text.strip()}\n")
        self.git(clone, "add", str(rel))
        self.git(clone, "-c", f"user.name={self.actor}", "-c", f"user.email={self.actor}@{self.c['hive_id']}.invalid",
                 "commit", "-q", "-m", f"{task['id']}: {kind} {n}")
        self.git(clone, "push", "-q", "origin", f"HEAD:{branch}")
        return self.pin(self.ws["repository"], self.git(clone, "rev-parse", "HEAD").strip(), str(rel), kind)


def default_tip(h, clone):
    """Where work in this repository starts from: the remote's default branch, not wherever the clone happens to
    be (a clone left on an earlier task's branch would make that branch the base, which the new work, started
    from main, doesn't contain: CODE_BASE_INVALID)."""
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master"):
        tip = h.git(clone, "rev-parse", "--verify", "-q", ref + "^{commit}", check=False).strip()
        if tip:
            return tip
    return h.git(clone, "rev-parse", "HEAD").strip()


def base_for(h, clone, head, recorded):
    """The base to pin with a result: the one recorded at accept if the work descends from it; otherwise where
    the work left the default branch (a task accepted while the clone was elsewhere)."""
    if recorded and subprocess.run(["git", "-C", clone, "merge-base", "--is-ancestor", recorded, head], env=h.env,
                                   capture_output=True).returncode == 0:
        return recorded
    fork = h.git(clone, "merge-base", head, default_tip(h, clone), check=False).strip()
    if not fork:
        raise Failed(f"your work in {clone} shares no history with its default branch: start it from there")
    log(f"base {(recorded or '?')[:12]} isn't an ancestor of {head[:12]}; using {fork[:12]}, where the work left the default branch")
    return fork


def pushed(h, clone, head):
    """Whether a commit is on a remote branch, without writing to the clone (a sidecar may run as another
    user than the agent): a branch tip on any remote is the commit, or contains it."""
    for remote in h.git(clone, "remote", check=False).split():
        out = h.run("git", "-C", clone, "ls-remote", "--heads", remote, check=False)
        for tip in {line.split()[0] for line in out.splitlines() if line.strip()}:
            if tip == head:
                return True
            if subprocess.run(["git", "-C", clone, "merge-base", "--is-ancestor", head, tip], env=h.env,
                              capture_output=True).returncode == 0:
                return True
    return bool(h.git(clone, "branch", "-r", "--contains", head, check=False).strip())


SKIP_DIRS = {".runtime", "node_modules", ".cache", ".venv", "venv", "__pycache__", ".npm", ".hive"}


def _norm_url(u):
    """github.com/owner/repo for any of a repository's URL forms (https, ssh, scp-like, with or without .git)."""
    u = re.sub(r"^[a-z+]+://", "", u.strip().lower())
    u = re.sub(r"^[^@/]*@", "", u)
    u = re.sub(r"^([^/:]+):\d+/", r"\1/", u)                   # host:port/path
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", u) else u   # scp-like host:path
    u = re.sub(r"^ssh\.github\.com/", "github.com/", u)          # GitHub's SSH over port 443
    return re.sub(r"(\.git)?/*$", "", u)


def _mark(tid, key):
    """Note a moment of the agent's work on a task (for the time split: record work versus the rest)."""
    f = TASKS / f"{tid}.json"
    try:
        d = json.loads(f.read_text()) if f.is_file() else {}
    except ValueError:
        d = {}
    d.setdefault("windows", []).append({key: time.time()})
    d[key] = time.time()
    f.write_text(json.dumps(d))


ORIGIN = re.compile(r"^telegram:-?\d+/\d+$")


def propose(h, st, text, origin):
    """A task someone asked for in chat, proposed to the chief of staff through the record (proposal.submitted with
    a complete task.created, approver class chief_of_staff), from any chat, private ones included. The order goes
    to the workspace; the chief of staff approves (the task is created) and assigns, or rejects with a reason."""
    title, _, body = text.partition("\n")
    title = title.strip().lstrip("#").strip()[:200]
    if not title:
        raise Failed("propose needs text: a title line, then what is wanted")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:28].strip("-") or "task"
    pid = f"p-{datetime.date.today().strftime('%m%d')}-{slug}-{os.urandom(2).hex()}"
    where = origin or "not given"
    order = (f"# {title}\n\nProposed by {h.actor}, asked in chat ({where}).\n\n{body.strip() or title}\n\n"
             "Route facts: specification=unknown verification=unknown scope=unknown consequence=unknown leverage=unknown\n")
    pin = json.loads(Path(h.order(pid, order)).read_text())
    proposed = {"type": "task.created", "task": pid, "basis": (st.get("hive") or {}).get("head", 0),
                "idempotency_key": f"proposal:{pid}", "refs": [{"rel": "order", "pin": pin}],
                "data": {"title": title, "ext": {"kind": "work"}}}
    if h.c.get("propose_goal"):        # a hive whose tasks need a goal (the 1-hive profile)
        proposed["goal"] = h.c["propose_goal"]
    args = ["emit", "proposal.submitted", "--data",
            json.dumps({"proposal_id": pid, "proposed": proposed, "approver_class": "chief_of_staff"})]
    if ORIGIN.match(origin):
        args += ["--cause-external", origin]
    out = h.hive(*args)
    try:
        e = json.loads(out)
    except ValueError:
        raise Failed(out.strip()[-400:])
    return (f"proposed {pid} at position {e['position']}: the chief of staff approves it (it becomes a task, "
            f"assigned on this channel) or says why not")


def find_task(h, st, task_id, roles=("owner",)):
    if task_id:
        t = st["tasks"].get(task_id)
        if not t:
            raise Failed(f"no task {task_id} on the record")
        return t
    mine = [t for t in st["tasks"].values() if t.get("owner") == h.actor and t["status"] in ("in_progress", "blocked")]
    if len(mine) != 1:
        raise Failed(f"say which task: you have {len(mine)} open tasks"
                     + (f" ({', '.join(t['id'] for t in mine)})" if mine else ""))
    return mine[0]


def do(h, req):
    """One queued action. Returns the line the agent sees."""
    st = h.state()
    action, text = req["action"], (req.get("text") or "").strip()
    if action == "propose":
        return propose(h, st, text, (req.get("extra") or "").strip())
    t = find_task(h, st, req.get("task", ""))
    tid = t["id"]
    if action == "accept":
        h.emit("task.accepted", tid, cause=h.last_event(tid, "task.assigned", "task.reassigned"))
        base = {}
        for r in (t.get("ext") or {}).get("repos") or []:
            try:
                base[r] = default_tip(h, h.clone(r))
            except Failed as e:
                raise Failed(f"accepted, but {e}; you can't post a result for {r} until then (accept again after cloning)")
        (TASKS / f"{tid}.json").write_text(json.dumps({"base": base, "accepted_at": time.time()}))
        return f"accepted {tid}" + (f"; base {', '.join(f'{r} {c[:12]}' for r, c in base.items())}" if base else "")
    if action in ("progress", "checkpoint"):
        if not text:
            raise Failed("a report needs text")
        e = h.emit("task.reported", tid, {"kind": action}, [("report", h.report(t, action, text))])
        return f"{action} report on {tid} at position {e['position']}"
    if action == "block":
        needs = req.get("extra") or "decision"
        if needs not in ("decision", "information", "access", "external"):
            raise Failed("block needs extra = decision, information, access or external")
        if not text:
            raise Failed("a question needs text")
        q = h.report(t, "question", text)
        e = h.emit("task.blocked", tid, {"needs": needs, "reason": text.splitlines()[0][:500]}, [("question", q)])
        return f"{tid} is blocked (needs {needs}) at position {e['position']}; you'll get the answer here"
    if action == "unblock":
        e = h.emit("task.unblocked", tid, cause=h.last_event(tid, "task.answered"))
        return f"{tid} unblocked at position {e['position']}"
    if action == "result":
        if not text:
            raise Failed("a result needs its report text")
        refs, base = [], {}
        f = TASKS / f"{tid}.json"
        if f.is_file():
            base = json.loads(f.read_text()).get("base", {})
        for r in (t.get("ext") or {}).get("repos") or []:
            if r not in base:
                raise Failed(f"no recorded base for {r}: accept the task through this tool first")
            clone = h.clone(r)
            head = h.git(clone, "rev-parse", "HEAD").strip()
            base[r] = base_for(h, clone, head, base[r])
            if not pushed(h, clone, head):
                raise Failed(f"push {r} first: its HEAD {head[:12]} is on no remote branch")
            refs += [("code", h.pin(r, head, name="code")), ("base", h.pin(r, base[r], name="base"))]
        e = h.emit("task.result_posted", tid, None, [("result", h.report(t, "result", text))] + refs,
                   cause=h.last_event(tid, "review.recorded", "task.answered", "task.assigned", "task.reassigned"))
        _mark(tid, "result_at")
        return f"result posted on {tid} at position {e['position']}; a reviewer will look at it"
    if action == "review":
        verdict = req.get("extra")
        if verdict not in ("passed", "failed", "needs_information"):
            raise Failed("review needs extra = passed, failed or needs_information")
        if t.get("assigned_reviewer") != h.actor or not t.get("current_result"):
            raise Failed(f"you aren't the assigned reviewer of {tid}'s current result")
        e = h.emit("review.recorded", tid, {"verdict": verdict, "result_event": t["current_result"]["event_id"]},
                   [("review", h.report(t, "review", text or verdict))], cause=t["current_result"]["event_id"])
        return f"review of {tid}: {verdict}, at position {e['position']}"
    raise Failed(f"unknown action {action}")


def poll(h):
    """Turn what changed on the record into messages for the agent. The first poll after the channel
    starts reports everything still waiting on the agent; later ones, only what changed."""
    st = h.state()
    seen = json.loads(SEEN.read_text()) if SEEN.is_file() else {}
    first = not SEEN.is_file()
    for t in sorted(st["tasks"].values(), key=lambda t: t["id"]):
        tid, prev, now = t["id"], seen.get(t["id"], {}), {}
        if t.get("owner") == h.actor:
            now["status"] = t["status"]
            if t["status"] in ("done", "cancelled") and prev.get("status") not in (None, "done", "cancelled"):
                why = "closed after a passed review" if t["status"] == "done" else "cancelled"
                tell(f"Task {tid} ({t.get('title', '')}) is {why}: nothing more to do on it.")
            if t["status"] == "assigned" and prev.get("status") != "assigned":
                tell(f"New task {tid} for you: {t.get('title', '')}\nOrder: {h.read_pin(t.get('order'))}\n\n"
                     f"Accept it with hive_task(action='accept', task='{tid}', text='', extra='') when you start.")
            b = t.get("block") or {}
            now["answered"] = bool(b.get("answered"))
            if now["answered"] and not prev.get("answered") and t["status"] == "blocked":
                ans = next((v for v in b.values() if isinstance(v, dict) and "commit_oid" in v), None)
                tell(f"Your question on {tid} was answered: {h.read_pin(ans) or b.get('note', '')}\n"
                     f"Continue with hive_task(action='unblock', task='{tid}', text='', extra='').")
            lr = t.get("latest_review") or {}
            now["review"] = lr.get("event_id")
            if (lr and lr.get("event_id") != prev.get("review") and lr.get("verdict") != "passed"
                    and (not first or t["status"] == "in_progress")):
                tell(f"Review of your result on {tid}: {lr.get('verdict')} ({lr.get('reviewer')})\n"
                     f"{h.read_pin(lr.get('review_pin'))}\n\nAddress it and post a new result.")
        cr = t.get("current_result") or {}
        if t.get("assigned_reviewer") == h.actor and t["status"] == "in_review" and cr:
            now["to_review"] = cr.get("event_id")
            if prev.get("to_review") != cr.get("event_id"):
                tell(f"Review {tid} ({t.get('title', '')}): its result by {cr.get('author')} against its order.\n"
                     f"Order: {h.read_pin(t.get('order'))}\n\nResult: {h.read_pin(cr.get('pin'))}\n\n"
                     f"Record your verdict with hive_task(action='review', task='{tid}', text='<findings>', "
                     f"extra='passed|failed|needs_information').")
        if now:
            seen[tid] = now
    # the agent's proposals: tell it when the chief of staff decides
    for p in (st.get("proposals") or {}).values():
        if p.get("proposer") != h.actor or p.get("status") not in ("approved", "rejected"):
            continue
        key = f"_proposal:{p['id']}"
        if key not in seen and not first:
            if p["status"] == "approved":
                tell(f"Your proposal {p['id']} was approved: it is a task now; its assignment comes on this channel.")
            else:
                tell(f"Your proposal {p['id']} was rejected: {p.get('reason') or 'no reason given'}. Tell the person "
                     "who asked, if they're waiting.")
        seen[key] = p["status"]
    # the supervisor's nudges, delivered as messages (a long-lived agent is talked to, not restarted)
    nudged_after = seen.get("_nudges")
    out = h.hive("events", "--after", str(nudged_after or 0), "--type", "task.nudged")
    evs = [json.loads(x) for x in out.splitlines() if x.strip()]
    for e in evs:
        if nudged_after is not None and e.get("task") in st["tasks"] and st["tasks"][e["task"]].get("owner") == h.actor:
            tell(f"Nudge on {e['task']} from the supervisor: {(e.get('data') or {}).get('reason', '')}\n"
                 f"Report what you're doing with hive_task(action='progress', task='{e['task']}', text='...', extra=''), "
                 f"or block with a question if you're stuck.")
    seen["_nudges"] = max([e["position"] for e in evs], default=nudged_after or 0)
    SEEN.write_text(json.dumps(seen))
    if first:
        mine = [t["id"] for t in st["tasks"].values()
                if t.get("owner") == h.actor and t["status"] in ("assigned", "in_progress", "blocked")]
        tell(f"hive channel connected as {h.actor}." + (f" Open tasks: {', '.join(mine)}." if mine else ""))


CODE_SUFFIXES = (".py", ".metta", ".sh", ".js", ".ts")   # untracked files that count as code


def code_state(h):
    """What the agent's code is now, in one line: its Iter folder's branch and commit, a hash of what's
    uncommitted (tracked changes plus untracked code files), a hash of the hive channel and tool it holds,
    and a hash of each file in config's "declare_files" (e.g. a memory system's flags)."""
    import hashlib

    def short(b):
        return hashlib.sha256(b).hexdigest()[:8]

    skip = [".runtime", ".hive", *h.c.get("declare_exclude", [])]
    parts = []
    try:
        top = h.git(str(ROOT), "rev-parse", "--show-toplevel").strip()
        branch = h.git(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
        head = h.git(top, "rev-parse", "--short=10", "HEAD").strip()
        here = os.path.relpath(ROOT, top)
        excl = [f":(exclude,glob){os.path.relpath(ROOT / s, top)}/**" for s in skip]
        diff = h.git(top, "diff", "HEAD", "--binary", "--", here, *excl)
        untracked = [p for p in h.git(top, "ls-files", "--others", "--exclude-standard", "--", here, *excl).splitlines()
                     if p.endswith(CODE_SUFFIXES)]
        blob = diff.encode() + b"".join(p.encode() + b"\0" + (Path(top) / p).read_bytes() for p in sorted(untracked))
        parts.append(f"{branch}@{head}" + (f"+{short(blob)}" if blob else ""))
    except (Failed, OSError, subprocess.TimeoutExpired):
        parts.append("not under git")
    adapter = b"".join((ROOT / p).read_bytes() for p in ("channels/hive.py", "tools/hive_task.py") if (ROOT / p).is_file())
    parts.append(f"hive adapter {short(adapter)}" if adapter else "no hive adapter")
    for name, path in sorted(h.c.get("declare_files", {}).items()):
        f = ROOT / path
        parts.append(f"{name} {short(f.read_bytes())}" if f.is_file() else f"{name} missing")
    return "; ".join(parts)


def declare(h):
    """Re-declare the agent when its code changed (record SPEC §6.2: agents re-declare on
    self-modification). The code state goes into the declaration's harness line; model and sandbox
    stay as declared. Only for an Iter agent's folder, or where config names the "harness" (hive-task's
    queue folder for an OpenClaw agent holds no code)."""
    if "harness" not in h.c and not (ROOT / "channels" / "hive.py").is_file():
        return
    actors = json.loads(h.hive("actors"))
    cur = next((a.get("declaration") or {} for a in actors if a.get("id") == h.actor), None)
    if not cur:
        return
    harness = f"{h.c.get('harness', 'iter')} {code_state(h)}"[:200]
    if cur.get("harness") == harness:
        return
    data = {"actor_id": h.actor, "declaration": {**cur, "harness": harness}}
    h.hive("emit", "actor.declared", "--data", json.dumps(data))
    log(f"declared: {harness}")


# ---------------------------------------------------------------- beacons -----------------------
# What the agent is doing now, from its own files, posted to the hive's Floor every few seconds (config
# "beacon"): never authoritative, and never the text of a prompt or message. Signed with the agent's record
# key under the prefix "hive-beacon1", which the record never accepts, so a beacon can't be replayed as a write.

DAY = 86400
TAIL_BYTES = 2_000_000                               # of a route hook log, read per beacon
SESSION_CHAT = re.compile(r"(?:group|channel|direct|dm):(-?\d+)")


def _stamp(p):
    """When a file last changed: its mtime, or its ctime if later (a rename into processing/ or completed/
    keeps the mtime of arrival but sets the ctime)."""
    try:
        st = os.stat(p)
        return max(st.st_mtime, st.st_ctime)
    except OSError:
        return None


def _epoch(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _episodes(hook, since):
    """[start, end or None, model] per route-hook episode ending (or, if open, starting) after `since`,
    oldest first: a "routed" line paired with the later "ended" of the same episode (as time_split.py)."""
    size = hook.stat().st_size
    with hook.open("rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode(errors="replace").splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]                            # the first line of a tail is cut
    open_, out = {}, []
    for line in lines:
        try:
            e = json.loads(line)
            ep, at = e.get("episode"), _epoch(e["at"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not ep:
            continue
        if e.get("event") == "routed":
            open_[ep] = [at, None, e.get("model")]
        elif e.get("event") == "ended" and ep in open_:
            r = open_.pop(ep)
            r[1] = at
            out.append(r)
    out = sorted([r for r in out + list(open_.values()) if (r[1] or r[0]) >= since], key=lambda r: r[0])
    return out[-200:]


def _outer_channel(d, since):
    """An Iter outer channel (inbox/, processing/, completed/): only each claimed request's chat and message
    ids, never its prompt."""
    names = {}
    for sub in ("processing", "inbox", "completed"):
        try:
            names[sub] = [n for n in os.listdir(d / sub) if n.endswith(".json")]
        except OSError as e:
            return {"problem": f"can't read {d / sub}: {e.strerror or e}"}
    reqs = []
    for n in sorted(names["processing"]):
        try:
            src = json.loads((d / "processing" / n).read_text()).get("source") or {}
            reqs.append({"chat_id": int(src["chat_id"]), "message_id": int(src["message_id"]),
                         "since": _stamp(d / "processing" / n)})
        except OSError:
            # a request in progress that this user can't read (a sidecar under another user): only that it is there
            reqs.append({"chat_id": None, "message_id": None, "since": _stamp(d / "processing" / n)})
        except (ValueError, KeyError, TypeError):
            continue
    done = sorted(t for t in (_stamp(d / "completed" / n) for n in names["completed"]) if t)
    return {"requests": reqs, "queued": len(names["inbox"]), "last_answer": done[-1] if done else None,
            "answers": [t for t in done if t >= since][-300:]}


def _openclaw(d):
    """An OpenClaw agent's sessions folder: the newest transcript's time, and the chat of the session
    updated last (from sessions.json's keys, e.g. agent:main:telegram:group:-100…), if it says."""
    try:
        files = [(t, n) for n in os.listdir(d) if n.endswith(".jsonl") for t in [_stamp(d / n)] if t]
    except OSError as e:
        return {"problem": f"can't read {d}: {e.strerror or e}"}
    out = {"last_step": max(files)[0] if files else None}
    try:
        sessions = json.loads((d / "sessions.json").read_text())
        newest = Path(max(files)[1]).stem if files else None

        def rank(kv):
            v = kv[1] if isinstance(kv[1], dict) else {}
            hit = newest and newest in (str(v.get("sessionId")), Path(str(v.get("sessionFile") or "")).stem)
            return (bool(hit), float(v.get("updatedAt") or 0))
        key = max(sessions.items(), key=rank)[0]
        m = SESSION_CHAT.search(key)
        if m:
            out["chat"] = {"chat_id": int(m.group(1))}
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return out


def beacon_body(conf, root, started, kind=None, extra=None):
    """The beacon (v 1) for the agent whose folder is `root`, from its files alone (no record needed)."""
    b, root, now = conf.get("beacon") or {}, Path(root), time.time()
    since = now - DAY
    kind = kind or ("iter" if (root / "channels" / "hive.py").is_file()
                    else "openclaw" if b.get("openclaw_sessions") else "agent")
    body = {"v": 1, "actor": conf["actor"], "kind": kind, "at": now, "started": started, "problem": None}
    problems = []
    watch = [Path(p) for p in b.get("watch", [])]
    if kind == "iter" and (root / "experience.json").exists():
        watch.insert(0, root / "experience.json")
    steps = list(map(_stamp, watch))
    hook = Path(b["route_hook"]) if b.get("route_hook") else root / "route-hook.jsonl"
    if b.get("route_hook") or hook.is_file():
        try:
            body["episodes"] = _episodes(hook, since)
        except OSError as e:
            problems.append(f"can't read {hook}: {e.strerror or e}")
    for key, read in (("iter_channel", lambda d: _outer_channel(d, since)), ("openclaw_sessions", _openclaw)):
        if b.get(key):
            got = read(Path(b[key]))
            problems.append(got.pop("problem", None))
            steps.append(got.pop("last_step", None))
            body.update(got)
    steps = [t for t in steps if t]
    if watch or b.get("openclaw_sessions"):
        body["last_step"] = max(steps) if steps else None
    if OUTBOX.is_dir():
        body["pending_actions"] = len(list(OUTBOX.glob("[0-9]*")))
    extra = dict(extra or {})
    problems.insert(0, extra.pop("problem", None))
    body.update(extra)
    body["problem"] = "; ".join(p for p in problems if p)[:200] or None
    return body


def beacon_send(conf, body, key=None):
    """POST a signed beacon to config's beacon.url; returns the HTTP status."""
    if key is None:
        from hiverecord.keys import SigningKey
        key = SigningKey.from_file(conf["key_file"])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sig = key.sign(f"hive-beacon1\n{conf['hive_id']}\n{at}\n{hashlib.sha256(raw).hexdigest()}".encode())
    req = urllib.request.Request(conf["beacon"]["url"], data=raw, method="POST", headers={
        "Content-Type": "application/json", "X-Hive-Key": key.public, "X-Hive-Signed-At": at, "X-Hive-Signature": sig})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def beacon_loop(conf, kind=None, extra=None):
    """Send a beacon every beacon.every seconds (default 30), for as long as the daemon runs. `extra` is
    a function giving fields to add (the reviewer runner's "reviewing" and "problem")."""
    try:
        from hiverecord.keys import SigningKey
        key = SigningKey.from_file(conf["key_file"])
    except (ImportError, OSError, ValueError, KeyError) as e:
        log(f"beacons off: {type(e).__name__}: {e}")
        return
    every, started, noted, failing = int(conf["beacon"].get("every", 30)), time.time(), 0.0, False
    while True:
        try:
            beacon_send(conf, beacon_body(conf, ROOT, started, kind, extra() if extra else None), key)
            if failing:
                log("beacons: sending again")
            failing = False
        except Exception as e:   # the Floor may be down or restarting: keep trying, log rarely
            failing = True
            if time.time() - noted >= 600:
                log(f"beacon failed: {type(e).__name__}: {e}")
                noted = time.time()
        time.sleep(every)


def start_beacon(conf, kind=None, extra=None):
    """A daemon thread for beacons, if config has a beacon.url: deliveries can block the main loop for minutes."""
    if not (conf.get("beacon") or {}).get("url"):
        return None
    t = threading.Thread(target=beacon_loop, args=(conf, kind, extra), daemon=True, name="beacon")
    t.start()
    return t


def daemon():
    global DELIVER
    _dirs()
    PID.write_text(str(os.getpid()))
    try:
        h = Hive(json.loads(CONF.read_text()))
    except (Failed, OSError, ValueError, KeyError) as e:
        tell(f"hive channel can't start: {e}. Fix {CONF}.")
        log(f"can't start: {e}")
        return
    start_beacon(h.c)
    DELIVER = h.c.get("deliver") or None             # e.g. ["openclaw", "agent", "exec", "--agent", "main", "--message", "{text}"]
    every = int(h.c.get("poll_seconds", 30))
    declare_every = int(h.c.get("declare_seconds", 600))   # 0: never
    next_poll = next_declare = 0.0
    while True:
        for path in sorted(OUTBOX.glob("[0-9]*")):
            try:
                req = json.loads(path.read_text())
                path.unlink()
            except (FileNotFoundError, ValueError):
                continue
            try:
                tell(f"OK: {do(h, req)}")
            except (Failed, subprocess.TimeoutExpired, KeyError) as e:
                tell(f"FAILED: {req.get('action')} {req.get('task') or ''}: {e}")
            log(f"did {req.get('action')} {req.get('task')}")
            next_poll = 0.0
        if time.time() >= next_poll:
            try:
                poll(h)
            except Exception as e:   # keep running through record or network errors
                log(f"poll failed: {type(e).__name__}: {e}")
            next_poll = time.time() + every
        if declare_every and time.time() >= next_declare:
            try:
                declare(h)
            except Exception as e:
                log(f"declare failed: {type(e).__name__}: {e}")
            next_declare = time.time() + declare_every
        time.sleep(1)


if __name__ == "__main__" and "--daemon" in sys.argv:
    args = sys.argv[1:]
    root = args[args.index("--root") + 1] if "--root" in args else ROOT
    conf = args[args.index("--config") + 1] if "--config" in args else None
    configure(root, conf)
    daemon()
