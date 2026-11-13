# Demo GIF of one spanhound investigation on the saved paymentUnreachable incident.
# Needs ClickHouse running (infra/start_clickhouse.sh) and the model API key in the environment.
# Run from the repo root: vhs docs/demo.tape
#
# If VHS's bundled Chromium fails to start (missing libnss3/libnspr4/libasound),
# point it at locally unpacked copies of those libraries:
#   LD_LIBRARY_PATH=~/.local/lib/chromium-deps/usr/lib/x86_64-linux-gnu vhs docs/demo.tape
# Running `sudo apt install libnss3 libasound2t64` once removes the need for it.

Output docs/demo.gif

Require uv

Set Shell "bash"
Set Theme "Catppuccin Mocha"
Set FontSize 18
Set Width 1200
Set Height 700
Set Framerate 10
Set TypingSpeed 40ms

# Setup, not recorded: load the saved incident into otel_test.
Hide
Type "export PS1='$ '" Enter
Type "cd $(git rev-parse --show-toplevel) && clear" Enter
Type "uv run python scripts/load_fixture.py paymentUnreachable" Enter
Wait+Line@120s /^\$$/
Type "export SPANHOUND_CH_DATABASE=otel_test" Enter
Type "clear" Enter
Sleep 500ms
Show

Type `uv run python scripts/investigate.py --alert "Customers report that checkout fails" --start "2026-11-10 18:57:08" --end "2026-11-10 18:58:44"`
Sleep 500ms
Enter

# The model call takes 20 to 110 seconds; skip the wait. The report is taller
# than the screen, and Wait+Screen only reads the top rows, so it never sees
# "stopped_by" (the last field). Wait for the prompt to return instead.
Hide
Wait+Line@180s /^\$$/
Sleep 1s
Show

Sleep 4s
