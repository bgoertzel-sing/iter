#!/bin/bash
# usage: run_tests.sh <repo-dir-to-test> <label>
# Copies the repo dir into a staged layout mirroring protocosmo2/iter-port/repos so the
# progress-not-pause harness finds ../src (outer channel contract, copied) and
# ../../tools/phase6_private_canary_runner.py (symlink, read-only source inspection).
set -e
SRCREPO=$1; LABEL=$2
S=/tmp/claude-1001/-home-openclaw/cb18a9ee-4e7f-4b55-a3d2-4d0a25ab7645/scratchpad
P=/home/openclaw/research-agent/projects/omegaclaw/protocosmo2
ST=$S/stage-$LABEL/protocosmo2
rm -rf $S/stage-$LABEL
mkdir -p $ST/iter-port/src $ST/tools
cp $P/iter-port/src/*.py $ST/iter-port/src/
ln -s $P/tools/phase6_private_canary_runner.py $ST/tools/phase6_private_canary_runner.py
rsync -a --exclude .git --exclude __pycache__ $SRCREPO/ $ST/iter-port/repos/
cd $ST/iter-port/repos
T="test_background_idle_deadline.py test_bg_llm_hang.py test_branch_gate_backoff.py test_close_unfinished.py test_invoke_structured_channel_results.py test_llm_concurrency_cap.py test_multi_branch.py test_progress_not_pause.py test_send_final.py test_text_only_fallback.py test_text_only_retry_cap.py test_thread_visibility.py test_channel_has_probe.py test_concurrency_fixes_20261012.py test_d11_d13_20261013.py"
python3 -m py_compile iter.py && echo "py_compile OK"
timeout 900 python3 -m pytest -q -p no:cacheprovider -rfs $T 2>&1 | tail -${TAILN:-45}
