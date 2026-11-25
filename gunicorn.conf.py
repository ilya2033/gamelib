# One process (keeps the login throttle and SQLite writes simple) with
# threads so a multi-GB upload or download doesn't block the UI.
bind = "0.0.0.0:8080"
workers = 1
worker_class = "gthread"
threads = 8
# Worker heartbeat timeout. With gthread, long uploads/downloads are not
# killed by this; it only restarts a worker that has hung completely.
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'
