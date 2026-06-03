"""Channel — generic append-only YAML queue with ack semantics, atomic writes, idempotency.

A channel file is: {<list_key>: [ envelope, ... ]} where envelope =
  {id, submitted_at, acked, acked_at, acked_by, answer, **payload}.
This is the shared interface behind the orchestrator inbox, committee queue, and GPU task/result channels.
"""
import os, sys, glob, datetime, tempfile, time, errno

NOW = lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _yaml():
    try:
        import yaml; return yaml
    except ImportError:
        sys.exit("ERROR: pyyaml required -> pip install pyyaml")
YAML = _yaml()

def _load(path, default):
    if not os.path.exists(path): return default
    try:
        with open(path) as f: return YAML.safe_load(f) or default
    except Exception:
        # parse-tolerant salvage (mirror ros.load_yaml): trim to last parseable prefix
        try:
            txt = open(path, errors="ignore").read()
            for cut in range(len(txt), 0, -1):
                try:
                    v = YAML.safe_load(txt[:cut])
                    if isinstance(v, (dict, list)): return v
                except Exception:
                    continue
        except Exception:
            pass
        return default

def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            YAML.safe_dump(obj, f, sort_keys=False, default_flow_style=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise

class _FileLock:
    """★ BUG-59 FIX: serialize a channel's read-modify-write under a per-channel O_EXCL lockfile.
    _dump is atomic per-write, but submit/ack do read-then-write — two concurrent submitters both read
    the same state, compute the SAME next id, append, and the 2nd os.replace CLOBBERS the 1st (silent
    data loss). Two parallel sub-monitor committee submits -> one lost; two GPU results land at once ->
    one lost (the orchestrator never drains it). This is the BUG-52 next_id race, in the Channel layer.
    Stale-lock recovery: a lock older than `stale_s` (holder crashed) is reclaimed."""
    def __init__(self, target_path, timeout=30.0, stale_s=60.0):
        self.lockpath = target_path + ".lock"
        self.timeout = timeout; self.stale_s = stale_s; self.fd = None
    def __enter__(self):
        os.makedirs(os.path.dirname(self.lockpath) or ".", exist_ok=True)
        start = time.time()
        while True:
            try:
                self.fd = os.open(self.lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(self.fd, f"{os.getpid()} {NOW()}\n".encode()); return self
            except OSError as e:
                if e.errno != errno.EEXIST: raise
                # reclaim a stale lock (crashed holder)
                try:
                    if time.time() - os.path.getmtime(self.lockpath) > self.stale_s:
                        os.unlink(self.lockpath); continue
                except OSError:
                    pass
                if time.time() - start > self.timeout:
                    raise TimeoutError(f"channel lock timeout: {self.lockpath}")
                time.sleep(0.02)
    def __exit__(self, *a):
        try:
            if self.fd is not None: os.close(self.fd)
        finally:
            try: os.unlink(self.lockpath)
            except OSError: pass

class Channel:
    """Generic queue/inbox. list_key is the YAML list field ('items','queue','tasks','results')."""
    def __init__(self, path, list_key="items", id_prefix="ITEM"):
        self.path = path; self.list_key = list_key; self.id_prefix = id_prefix

    def _read(self):
        d = _load(self.path, {self.list_key: []}) or {self.list_key: []}
        if self.list_key not in d or not isinstance(d.get(self.list_key), list):
            d[self.list_key] = []
        return d

    def _next_id(self, d):
        return f"{self.id_prefix}-{len(d[self.list_key])+1:04d}"

    def submit(self, payload, dedup_keys=None, mirror_id_key=None):
        """Append an envelope. If dedup_keys given, skip (return existing id) when an UN-ACKED item
        already matches all those payload keys (idempotency for retried submits).
        If mirror_id_key given, also store the assigned id under that key on the envelope (e.g.
        'queue_id' for back-compat readers) — done INSIDE the lock so it can't race a concurrent submit.
        ★ BUG-59: the whole read-modify-write runs under a per-channel lock so concurrent submits can't
        collide on the same id / clobber each other."""
        with _FileLock(self.path):
            d = self._read()
            if dedup_keys:
                for it in d[self.list_key]:
                    if it.get("acked"): continue
                    if all(it.get(k) == payload.get(k) for k in dedup_keys):
                        return {"id": it.get("id"), "dup": True}
            iid = self._next_id(d)
            env = {"id": iid, "submitted_at": NOW(), "acked": False,
                   "acked_at": "", "acked_by": "", "answer": "", **payload}
            if mirror_id_key: env[mirror_id_key] = iid
            d[self.list_key].append(env); _dump(self.path, d)
            return {"id": iid, "dup": False}

    def list(self, include_acked=False, predicate=None):
        d = self._read()
        out = [i for i in d[self.list_key] if (include_acked or not i.get("acked"))]
        if predicate: out = [i for i in out if predicate(i)]
        return out

    def ack(self, item_id=None, all_items=False, by="", answer="", mirror_key=None,
            match_field=None, match_val=None):
        # ★ BUG-59: ack is also read-modify-write -> serialize it (a concurrent submit during ack could
        # otherwise lose either the ack or the new item). mirror_key matches legacy items whose id lives
        # under an alias key (e.g. 'queue_id') with no native 'id'.
        # ★ BUG-97: (match_field, match_val) acks ALL un-acked items where item[match_field]==match_val
        # (e.g. per-agent inbox ack) — INSIDE the lock, so a concurrent submit can't be clobbered by an
        # unlocked read-modify-write (the inbox-ack analogue of the BUG-59 queue-ack fix).
        with _FileLock(self.path):
            d = self._read(); n = 0
            for i in d[self.list_key]:
                if i.get("acked"): continue
                if (all_items or i.get("id") == item_id or (mirror_key and i.get(mirror_key) == item_id)
                        or (match_field is not None and i.get(match_field) == match_val)):
                    i["acked"] = True; i["acked_at"] = NOW(); i["acked_by"] = by; i["answer"] = answer; n += 1
            _dump(self.path, d); return n
