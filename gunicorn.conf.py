# Read automatically by gunicorn from the directory it starts in, so it
# applies whatever start command the host is configured with (render.yaml
# or the dashboard); flags on the command line still take precedence.
#
# One process, several threads. One process because the rate limiter,
# report list and caches live in its memory, and because a second copy
# of the 2M-node road graph would not fit in Render's 512 MB. Threads
# because a single sync worker served one request at a time: while one
# evacuation computed (~6 s), every other page -- /status included --
# waited behind it. Route computations are still serialised inside the
# app (see ONE ROUTE AT A TIME in server.py) to bound memory.

workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
