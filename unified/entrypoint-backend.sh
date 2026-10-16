#!/bin/sh
# Launches supervisord in the foreground.
# supervisord manages piper, supertonic, and the UI server (see /etc/supervisord.conf).
# The UI is gated behind wait_health.py so it only binds after backends are ready.
set -e
exec supervisord -n -c /etc/supervisord.conf
