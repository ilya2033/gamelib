"""Tiny in-process background job runner.

Slow share work (walking folders, detecting platforms, measuring sizes)
runs in daemon threads so web requests return immediately. Jobs report
progress, can be cancelled, and are kept in memory (the last KEEP ones).

This relies on a single gunicorn worker process (see gunicorn.conf.py),
so every request sees the same job list. With JOBS_SYNC (tests) a job
runs inline before start() returns."""
import logging
import threading
import time
import uuid

from flask import current_app

log = logging.getLogger("gamelib")
KEEP = 30


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind, label):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.label = label
        self.state = "running"          # running | done | failed | cancelled
        self.done = 0
        self.total = 0
        self.message = ""
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None
        self.seen = False               # result shown to the user once
        self._cancel = threading.Event()

    @property
    def running(self):
        return self.state == "running"

    def progress(self, done=None, total=None, message=None):
        if total is not None:
            self.total = total
        if done is not None:
            self.done = done
        if message is not None:
            self.message = message

    def cancel(self):
        self._cancel.set()

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "state": self.state,
                "done": self.done, "total": self.total, "message": self.message,
                "error": self.error, "elapsed": round((self.finished or time.time()) - self.started, 1)}


_jobs = {}
_lock = threading.Lock()


def start(kind, label, fn, *args, exclusive=True):
    """Run fn(job, *args) in the background. With exclusive, a job of the
    same kind that is still running is returned instead of a new one."""
    app = current_app._get_current_object()
    with _lock:
        if exclusive:
            for j in _jobs.values():
                if j.kind == kind and j.running:
                    return j
        job = Job(kind, label)
        _jobs[job.id] = job
        finished = sorted((j for j in _jobs.values() if not j.running), key=lambda j: j.started)
        for old in finished[:max(0, len(_jobs) - KEEP)]:
            _jobs.pop(old.id, None)
    if app.config.get("JOBS_SYNC"):
        _run(app, job, fn, args)
    else:
        threading.Thread(target=_run, args=(app, job, fn, args), daemon=True, name=f"job-{kind}").start()
    return job


def _run(app, job, fn, args):
    with app.app_context():
        try:
            job.result = fn(job, *args)
            job.state = "done"
        except Cancelled:
            job.state = "cancelled"
        except Exception as e:  # report to the user, keep the app running
            log.exception("Background job %s (%s) failed", job.kind, job.id)
            job.error = str(e) or e.__class__.__name__
            job.state = "failed"
        finally:
            job.finished = time.time()


def get(job_id):
    with _lock:
        return _jobs.get(job_id)


def running(kind):
    with _lock:
        return next((j for j in _jobs.values() if j.kind == kind and j.running), None)
