#!/bin/bash
# Pipeline runner with monitoring and auto-restart
# Usage: nohup ./scripts/run_pipelines.sh &> logs/pipelines.log &
#
# Runs all active pipelines in parallel, restarts on crash,
# and prints status every hour.

cd "$(dirname "$0")/.."
source .venv/bin/activate

LOGDIR="logs"
mkdir -p "$LOGDIR"

# --- Pipeline functions (each runs in a loop until done) ---

run_crossref_v2() {
    while true; do
        echo "[$(date)] Starting/resuming Crossref v2..."
        python scripts/02_crossref_collect_v2.py \
            --resume --email reviewtimedb@rapidpeer.com \
            --journal-list data/journal_list_full.csv \
            2>&1 | tee -a "$LOGDIR/crossref_v2.log"
        EXIT_CODE=${PIPESTATUS[0]}
        if [ $EXIT_CODE -eq 0 ]; then
            echo "[$(date)] Crossref v2 completed successfully."
            return 0
        fi
        echo "[$(date)] Crossref v2 exited with code $EXIT_CODE. Restarting in 10s..."
        sleep 10
    done
}

run_tier1() {
    while true; do
        echo "[$(date)] Starting/resuming Tier 1 scraper..."
        python scripts/05_scrape_publishers.py \
            --tier 1 --resume \
            --journal-list data/journal_list_full.csv \
            2>&1 | tee -a "$LOGDIR/tier1.log"
        EXIT_CODE=${PIPESTATUS[0]}
        if [ $EXIT_CODE -eq 0 ]; then
            echo "[$(date)] Tier 1 scraper completed successfully."
            return 0
        fi
        echo "[$(date)] Tier 1 exited with code $EXIT_CODE. Restarting in 10s..."
        sleep 10
    done
}

run_tier2() {
    while true; do
        echo "[$(date)] Starting/resuming Tier 2 scraper..."
        python scripts/05_scrape_publishers.py \
            --tier 2 --resume \
            --journal-list data/journal_list_full.csv \
            2>&1 | tee -a "$LOGDIR/tier2.log"
        EXIT_CODE=${PIPESTATUS[0]}
        if [ $EXIT_CODE -eq 0 ]; then
            echo "[$(date)] Tier 2 scraper completed successfully."
            return 0
        fi
        echo "[$(date)] Tier 2 exited with code $EXIT_CODE. Restarting in 10s..."
        sleep 10
    done
}

monitor() {
    while true; do
        sleep 3600
        echo ""
        echo "=== [$(date)] Pipeline Health Check ==="

        CR_DONE=$(python3 -c "import json; d=json.load(open('data/crossref_checkpoint_v2.json')); print(len(d))" 2>/dev/null || echo 0)
        T1_DONE=$(python3 -c "import json; d=json.load(open('data/scrape_checkpoint_t1.json')); print(len(d))" 2>/dev/null || echo 0)
        T2_DONE=$(python3 -c "import json; d=json.load(open('data/scrape_checkpoint_t2.json')); print(len(d))" 2>/dev/null || echo 0)

        echo "  Crossref v2: ${CR_DONE}/28789 journals"
        echo "  Tier 1: ${T1_DONE} journals"
        echo "  Tier 2: ${T2_DONE} journals"
        echo ""
    done
}

# --- Launch all in parallel ---

echo "=== [$(date)] Starting all pipelines ==="

run_crossref_v2 &
PID_CR=$!

run_tier1 &
PID_T1=$!

run_tier2 &
PID_T2=$!

monitor &
PID_MON=$!

echo "  Crossref v2: PID $PID_CR"
echo "  Tier 1: PID $PID_T1"
echo "  Tier 2: PID $PID_T2"
echo "  Monitor: PID $PID_MON"
echo ""
echo "To stop all: kill $PID_CR $PID_T1 $PID_T2 $PID_MON"

# Wait for all pipelines to finish
wait $PID_CR $PID_T1 $PID_T2

# Kill monitor once pipelines are done
kill $PID_MON 2>/dev/null

echo ""
echo "=== [$(date)] All pipelines complete ==="
