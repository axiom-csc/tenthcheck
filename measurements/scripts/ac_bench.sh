#!/bin/zsh
# Run bench_latency.py only on continuous AC power (macOS laptops); abort and retry if power drops.
#
#   measurements/scripts/ac_bench.sh [corpus_dir] [python]
#
# corpus_dir defaults to ~/tenthcheck-corpus; python defaults to `python3` (it needs tenthcheck[mcp] installed,
# and the `tenthcheck` command next to it). Waits for 2 minutes of uninterrupted AC, checks the power source
# every 5 s during the run, and writes results/latency_<date>_ac.json only if the run stayed on AC throughout.
set -u
HERE=${0:A:h}
MEAS=${HERE:h}
CORPUS=${1:-$HOME/tenthcheck-corpus}
PY=${2:-python3}
OUT=$MEAS/results/latency_$(date +%Y-%m-%d)_ac.json
LOG=${TMPDIR:-/tmp}/tenthcheck_ac_bench.log
on_ac() { pmset -g batt | head -1 | grep -q "AC Power"; }

for attempt in 1 2 3; do
  stable=0
  while [ $stable -lt 24 ]; do  # 24 x 5 s = 2 min of uninterrupted AC
    if on_ac; then stable=$((stable+1)); else stable=0; fi
    sleep 5
  done
  echo "attempt $attempt: AC stable 2 min, starting $(date '+%H:%M:%S')"
  (cd $MEAS && $PY scripts/bench_latency.py $CORPUS $OUT.tmp) > $LOG 2>&1 &
  pid=$!
  dropped=0
  while kill -0 $pid 2>/dev/null; do
    if ! on_ac; then dropped=1; pkill -P $pid; kill $pid 2>/dev/null; wait $pid 2>/dev/null; break; fi
    sleep 5
  done
  if [ $dropped -eq 1 ]; then echo "attempt $attempt: power dropped at $(date '+%H:%M:%S'), aborted"; rm -f $OUT.tmp; continue; fi
  wait $pid; rc=$?
  if [ $rc -ne 0 ]; then echo "attempt $attempt: benchmark failed rc=$rc (log: $LOG)"; tail -5 $LOG; exit 1; fi
  if on_ac; then mv $OUT.tmp $OUT; echo "done, on AC throughout, $(date '+%H:%M:%S'): $OUT"; exit 0; fi
  echo "attempt $attempt: on battery at finish, discarded"; rm -f $OUT.tmp
done
echo "gave up after 3 attempts"; exit 2
