"""Keep a Render free web service from sleeping.

Render's free plan freezes a service after ~15 minutes without *inbound*
HTTP traffic. A long-polling Telegram bot only makes outbound requests, so
Render would put it to sleep. This module starts a background thread that
periodically requests the service's own public URL; the request loops back
through Render's load balancer, which resets the idle timer.

Every ping is logged (flushed immediately) so the Render app logs prove the
keep-alive is alive — grep for "keep_alive".

Env (Render injects these):
  RENDER_EXTERNAL_URL  public URL, e.g. https://<service>.onrender.com
  PORT                 local healthcheck port (fallback only — a localhost
                       ping bypasses the balancer and does NOT reset sleep)
  KEEPALIVE_INTERVAL   seconds between pings (default 600; capped at 840 so
                       it always fires inside Render's 900s idle window)
"""
import os
import threading
import time
import urllib.request

DEFAULT_INTERVAL = 600   # 10 min, well under Render's ~15 min idle window
MAX_INTERVAL = 840       # hard ceiling: must stay below the 900s threshold
HEALTH_PATH = "/health"


def _ping(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print(f"[keep_alive] ping failed: {e}", flush=True)
        return False


def _loop(url, interval):
    while True:
        # ping first, then sleep — so a fresh deploy counts immediately
        ok = _ping(url)
        print(f"[keep_alive] ping {'ok' if ok else 'FAILED'} -> {url}", flush=True)
        time.sleep(interval)


def start():
    """Return the keep-alive thread, or None when not running on Render."""
    external = os.environ.get("RENDER_EXTERNAL_URL")
    port = os.environ.get("PORT")
    url = None
    if external:
        url = external.rstrip("/") + HEALTH_PATH
    elif port:
        # Fallback for other PaaS; note this does not reset Render's idle
        # timer (it bypasses the balancer), but keeps the local check warm.
        url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    if not url:
        return None

    interval = DEFAULT_INTERVAL
    try:
        interval = int(os.environ.get("KEEPALIVE_INTERVAL", str(DEFAULT_INTERVAL)))
    except ValueError:
        pass
    interval = max(10, min(interval, MAX_INTERVAL))

    print(f"[keep_alive] started, interval={interval}s, url={url}", flush=True)
    t = threading.Thread(target=_loop, args=(url, interval), daemon=True)
    t.start()
    return t
