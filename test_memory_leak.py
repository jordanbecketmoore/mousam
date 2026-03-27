#!/usr/bin/env python3
"""
Memory leak test for mousam auto-refresh.

Starts (or attaches to) a running mousam process, sets the auto-refresh
interval to 1 minute, samples RSS every 10 seconds for a configurable
duration, then reports whether memory is growing or stable.

Usage:
    python3 test_memory_leak.py [--duration MINUTES] [--interval REFRESH_MINUTES]

Examples:
    python3 test_memory_leak.py               # 10-minute test, 1-minute refresh
    python3 test_memory_leak.py --duration 20 # 20-minute test
"""

import argparse
import subprocess
import sys
import time
import signal
import os

GSETTINGS_SCHEMA = "io.github.amit9838.mousam"
GSETTINGS_KEY = "auto-refresh-interval"
SAMPLE_INTERVAL_S = 10  # how often to read RSS


def find_mousam_pid():
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "mousam"], text=True
        ).strip()
        pids = [int(p) for p in out.splitlines() if p.strip()]
        return pids[0] if pids else None
    except subprocess.CalledProcessError:
        return None


def get_rss_kb(pid):
    """Read RSS from /proc/<pid>/status (VmRSS line), in KB."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ValueError):
        return None
    return None


def gsettings_get():
    out = subprocess.check_output(
        ["gsettings", "get", GSETTINGS_SCHEMA, GSETTINGS_KEY], text=True
    ).strip()
    return int(out)


def gsettings_set(value):
    subprocess.check_call(
        ["gsettings", "set", GSETTINGS_SCHEMA, GSETTINGS_KEY, str(value)]
    )


def start_mousam():
    """Launch mousam in the background and return the process."""
    proc = subprocess.Popen(
        ["python3", "-m", "src.main"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def linear_trend(samples):
    """
    Returns bytes-per-second growth rate via least-squares fit.
    Positive = growing, negative = shrinking.
    """
    n = len(samples)
    if n < 2:
        return 0.0
    times = [s[0] for s in samples]
    values = [s[1] for s in samples]
    t0 = times[0]
    xs = [t - t0 for t in times]
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    num = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    return (num / den) if den != 0 else 0.0


def format_rss(kb):
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=10, metavar="MINUTES",
                        help="How long to run the test (default: 10 minutes)")
    parser.add_argument("--interval", type=int, default=1, metavar="REFRESH_MINUTES",
                        help="Auto-refresh interval to set (default: 1 minute)")
    args = parser.parse_args()

    test_duration_s = args.duration * 60
    refresh_interval = args.interval

    # --- Find or start mousam ---
    spawned = None
    pid = find_mousam_pid()
    if pid:
        print(f"Found running mousam (PID {pid})")
    else:
        print("mousam not running — starting it...")
        spawned = start_mousam()
        time.sleep(3)  # give it time to initialize
        pid = find_mousam_pid()
        if not pid:
            print("ERROR: Could not start mousam or find its PID.")
            sys.exit(1)
        print(f"mousam started (PID {pid})")

    # --- Save original gsettings value ---
    try:
        original_interval = gsettings_get()
    except Exception as e:
        print(f"ERROR: Could not read GSettings ({e}). Is the schema installed?")
        sys.exit(1)

    print(f"Saved original auto-refresh-interval: {original_interval} minutes")

    def restore_and_exit(signum=None, frame=None):
        print(f"\nRestoring auto-refresh-interval to {original_interval}...")
        gsettings_set(original_interval)
        if spawned:
            spawned.terminate()
        sys.exit(0 if signum is None else 1)

    signal.signal(signal.SIGINT, restore_and_exit)
    signal.signal(signal.SIGTERM, restore_and_exit)

    # --- Set fast refresh interval ---
    print(f"Setting auto-refresh-interval to {refresh_interval} minute(s)...")
    gsettings_set(refresh_interval)

    # --- Sample loop ---
    samples = []  # list of (timestamp, rss_kb)
    deadline = time.monotonic() + test_duration_s
    expected_cycles = test_duration_s // (refresh_interval * 60)

    print(f"\nSampling RSS every {SAMPLE_INTERVAL_S}s for {args.duration} minutes")
    print(f"Expected refresh cycles during test: ~{expected_cycles}")
    print(f"{'Elapsed':>10}  {'RSS':>10}  {'Delta from start':>16}")
    print("-" * 42)

    start_rss = None
    while time.monotonic() < deadline:
        rss = get_rss_kb(pid)
        if rss is None:
            print("ERROR: mousam process disappeared.")
            restore_and_exit()

        now = time.monotonic()
        samples.append((now, rss))

        if start_rss is None:
            start_rss = rss

        elapsed = now - (deadline - test_duration_s)
        delta = rss - start_rss
        sign = "+" if delta >= 0 else ""
        print(f"{elapsed:>9.0f}s  {format_rss(rss):>10}  {sign}{format_rss(abs(delta)):>15}")

        # Sleep in small increments so Ctrl-C is responsive
        sleep_end = time.monotonic() + SAMPLE_INTERVAL_S
        while time.monotonic() < sleep_end:
            time.sleep(0.5)

    # --- Analysis ---
    rss_values = [s[1] for s in samples]
    growth_rate_kb_s = linear_trend(samples)
    growth_rate_kb_per_cycle = growth_rate_kb_s * refresh_interval * 60

    print("\n" + "=" * 42)
    print("RESULTS")
    print("=" * 42)
    print(f"  Start RSS:          {format_rss(rss_values[0])}")
    print(f"  End RSS:            {format_rss(rss_values[-1])}")
    print(f"  Min RSS:            {format_rss(min(rss_values))}")
    print(f"  Max RSS:            {format_rss(max(rss_values))}")
    print(f"  Net change:         {'+' if rss_values[-1] >= rss_values[0] else ''}{format_rss(rss_values[-1] - rss_values[0])}")
    print(f"  Growth rate:        {growth_rate_kb_s * 1024 / 1024:.1f} KB/s")
    print(f"  Growth per cycle:   {growth_rate_kb_per_cycle:.0f} KB/cycle (~{growth_rate_kb_per_cycle/1024:.2f} MB/cycle)")
    print()

    # Verdict thresholds (per refresh cycle):
    #   >5 MB  — definite leak, old widget trees not being freed
    #   1-5 MB — likely pymalloc arena fragmentation; run longer to confirm plateau
    #   <1 MB  — acceptable; within normal OS/allocator overhead
    LEAK_THRESHOLD_KB = 5 * 1024
    WARN_THRESHOLD_KB = 1 * 1024
    if growth_rate_kb_per_cycle > LEAK_THRESHOLD_KB:
        print(f"VERDICT: LIKELY LEAK — {growth_rate_kb_per_cycle:.0f} KB/cycle (old widget trees not freed)")
        verdict = 1
    elif growth_rate_kb_per_cycle > WARN_THRESHOLD_KB:
        print(f"VERDICT: MARGINAL — {growth_rate_kb_per_cycle:.0f} KB/cycle (likely pymalloc arena overhead; run --duration 30 to confirm plateau)")
        verdict = 0
    elif growth_rate_kb_per_cycle > 0:
        print(f"VERDICT: STABLE — {growth_rate_kb_per_cycle:.0f} KB/cycle (normal OS/allocator overhead)")
        verdict = 0
    else:
        print("VERDICT: STABLE — no sustained memory growth detected")
        verdict = 0

    restore_and_exit()
    return verdict


if __name__ == "__main__":
    sys.exit(main())
