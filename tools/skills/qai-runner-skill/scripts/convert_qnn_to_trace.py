# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#!/usr/bin/env python3
import json
import argparse
from pathlib import Path

def main():
    p = argparse.ArgumentParser(description="Convert QNN JSON profile output to standard Google Chrome Trace format.")
    p.add_argument("--input", type=Path, default=Path("qairt_profile_output/profile_json.json"), help="Path to input QNN profile JSON")
    p.add_argument("--output", type=Path, default=Path("qairt_profile_output/chromeTrace.json"), help="Path to output Chrome Trace JSON")
    p.add_argument("--time-us", type=float, default=33079.0, help="Default total execution time in microseconds")
    p.add_argument("--cycles", type=float, default=42136927.0, help="Default total execution cycles")
    args = p.parse_args()

    if not args.input.exists():
        print(f"Error: Input file {args.input} does not exist.")
        return

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    trace_events = []
    messages = data.get("messages", [])

    acc_execute_time_us = args.time_us
    acc_execute_cycles = args.cycles

    # Dynamic auto-discovery of cycles and time metrics
    discovered_cycles = None
    discovered_time = None
    for msg in messages:
        if msg.get("method") == "BACKEND_EXECUTE":
            events = msg.get("profilingEvents", [])
            for evt in events:
                ident = evt.get("identifier", "")
                if "Accelerator (execute) time (cycles)" in ident:
                    discovered_cycles = float(evt.get("value", 0))
                elif "QNN accelerator (execute) time" in ident:
                    # Metric is typically in microseconds
                    discovered_time = float(evt.get("value", 0))

    if discovered_cycles and discovered_cycles > 0:
        acc_execute_cycles = discovered_cycles
    if discovered_time and discovered_time > 0:
        acc_execute_time_us = discovered_time

    cycle_to_us_scale = acc_execute_time_us / acc_execute_cycles
    print(f"Using scale factor: {cycle_to_us_scale:.8f} us/cycle (Time: {acc_execute_time_us} us, Cycles: {acc_execute_cycles})")

    for msg in messages:
        method = msg.get("method")
        app_metrics = msg.get("appMetrics", {})
        start = app_metrics.get("startTime", 0)
        stop = app_metrics.get("stopTime", 0)
        
        if method == "APP_CONTEXT_CREATE":
            trace_events.append({
                "name": "Context Init",
                "cat": "LIFECYCLE",
                "ph": "X",
                "ts": 0,
                "dur": (stop - start),
                "pid": 1,
                "tid": 0
            })
        elif method == "APP_COMPOSE_GRAPHS":
            trace_events.append({
                "name": "Compose Graphs",
                "cat": "LIFECYCLE",
                "ph": "X",
                "ts": 10000,
                "dur": (stop - start),
                "pid": 1,
                "tid": 0
            })
        elif method == "BACKEND_FINALIZE":
            trace_events.append({
                "name": "Backend Finalize (Compile)",
                "cat": "LIFECYCLE",
                "ph": "X",
                "ts": 20000,
                "dur": (stop - start),
                "pid": 1,
                "tid": 0
            })
        elif method == "BACKEND_EXECUTE":
            exec_start_ts = 1000000.0 # Offset execution trace to ts = 1,000,000 us for clarity
            trace_events.append({
                "name": "HTP Execution (Overall)",
                "cat": "EXECUTE",
                "ph": "X",
                "ts": exec_start_ts,
                "dur": acc_execute_time_us,
                "pid": 1,
                "tid": 1
            })

            events = msg.get("profilingEvents", [])
            for evt in events:
                if "Accelerator (execute) time (cycles)" in evt.get("identifier", ""):
                    sub_evts = evt.get("sub-events", [])
                    current_op_ts = exec_start_ts
                    for sub in sub_evts:
                        op_name = sub.get("identifier", "Unknown Op")
                        cycles = sub.get("value", 0)
                        
                        if cycles > 0:
                            dur_us = cycles * cycle_to_us_scale
                            trace_events.append({
                                "name": op_name.replace(" (cycles)", ""),
                                "cat": "OP",
                                "ph": "X",
                                "ts": current_op_ts,
                                "dur": dur_us,
                                "pid": 1,
                                "tid": 2,
                                "args": {"cycles": cycles}
                            })
                            current_op_ts += dur_us

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"traceEvents": trace_events}, f, indent=4)
        
    print(f"Successfully converted and saved {len(trace_events)} trace events to {args.output}")

if __name__ == "__main__":
    main()
