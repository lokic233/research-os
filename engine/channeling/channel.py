"""Channel — generic append-only YAML queue with ack semantics, atomic writes, idempotency.

A channel file is: {<list_key>: [ envelope, ... ]} where envelope =
  {id, submitted_at, acked, acked_at, acked_by, answer, **payload}.
This is the shared interface behind the orchestrator inbox, committee queue, and GPU task/result channels.
"""
import os, sys, glob, datetime, tempfile

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

    def submit(self, payload, dedup_keys=None):
        """Append an envelope. If dedup_keys given, skip (return existing id) when an UN-ACKED item
        already matches all those payload keys (idempotency for retried submits)."""
        d = self._read()
        if dedup_keys:
            for it in d[self.list_key]:
                if it.get("acked"): continue
                if all(it.get(k) == payload.get(k) for k in dedup_keys):
                    return {"id": it.get("id"), "dup": True}
        iid = self._next_id(d)
        env = {"id": iid, "submitted_at": NOW(), "acked": False,
               "acked_at": "", "acked_by": "", "answer": "", **payload}
        d[self.list_key].append(env); _dump(self.path, d)
        return {"id": iid, "dup": False}

    def list(self, include_acked=False, predicate=None):
        d = self._read()
        out = [i for i in d[self.list_key] if (include_acked or not i.get("acked"))]
        if predicate: out = [i for i in out if predicate(i)]
        return out

    def ack(self, item_id=None, all_items=False, by="", answer=""):
        d = self._read(); n = 0
        for i in d[self.list_key]:
            if not i.get("acked") and (all_items or i.get("id") == item_id):
                i["acked"] = True; i["acked_at"] = NOW(); i["acked_by"] = by; i["answer"] = answer; n += 1
        _dump(self.path, d); return n
