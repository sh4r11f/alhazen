# Pre-session checkout

Run this on the rig machine, with the probe, the solenoid, the DAQ and
SpikeGLX all connected the way they will be for the session — not from a
laptop that only has the config file.

```bash
alhazen check-rig --rig configs/rig-lab.yaml --pulse
```

`--pulse` is what tells the difference between "the SDK imports" and "the
pump is plugged in and the sorter is actually publishing": without it, reward
and sync only get constructed, not fired, and the sorted-stream spike check
cannot pass at all (it has nothing to listen for). Always run with `--pulse`
before a real session; the no-pulse form is for CI and machines with no
hardware attached.

It prints one line per device, `OK` or `FAIL`, and keeps going after a
failure so you get the whole picture in one pass rather than fixing one thing
and re-running to find the next. Exit code is non-zero if anything failed.

## What each line means, and what to physically check against it

- **config** — the YAML parsed. Always OK if you get any output at all.
- **monitor** — PsychoPy's stored registration for this panel agrees with the
  rig file. If this fails, run `alhazen monitor register --rig <yaml>` — do
  not start a session on a drifted registration, stimulus sizes will be
  wrong.
- **data_root** — the session's data directory is writable *now*. A FAIL here
  is a mount or permissions problem; fix it before, not after, the subject is
  in the chair.
- **eyetracker** — for EyeLink, printed with the host IP: confirm that's the
  address on the tracker subnet you expect. For TRACKPixx3, no address is
  printed (it's inside the display chassis) — OK means it responded, nothing
  else to check by hand. `mouse_sim` always reports OK with no hardware
  behind it.
- **reward** — fires one real 50 ms pulse when `--pulse` is given. Physically
  confirm: **do you hear the valve click / see fluid at the spout?** A
  software OK with no audible click means the solenoid line, not the
  software, is the problem — check the NI-DAQ wiring on the printed
  `device/channel` (e.g. `Dev1/ao0`).
- **sync** — fires one pulse per configured event line. Physically confirm:
  **does each line show a pulse on the scope/recorder input it's wired to?**
  The check reports how many lines it pulsed; count them against your wiring
  diagram. `backend: none` or `simulated` never touches hardware and says so.
- **recording** — checks that SpikeGLX's configured `data_dir` (the
  acquisition host's share) is reachable, not that SpikeGLX is running. FAIL
  here is almost always a share that didn't mount — check that before
  anything else on this line.
- **spikes** — this is the one that depends on something outside alhazen:
  - `simulated`: always OK, no hardware involved.
  - `spikeglx`: connects to SpikeGLX's remote command server (Options →
    Command Server, default port 4142) and confirms a stream is running.
    FAIL means either SpikeGLX isn't running, the command server is
    disabled, or the named `stream`/`channels` don't exist on the current
    acquisition — start/fix SpikeGLX and re-run, don't chase this in
    alhazen.
  - `sorted_stream`: **the one piece that comes from outside these repos.**
    alhazen only subscribes; a separate real-time spike-sorter process must
    already be publishing sorted units on the configured ZeroMQ address
    (`tcp://host:port`) before this check runs. The check listens for a
    `units` re-announcement and reports OK with the lag ("how far behind
    real time the sorter's output is") on success. On failure it
    distinguishes two cases in the message: nothing published at all within
    the heartbeat timeout ("is the sorter running?") vs. something is
    publishing but never re-announced `units` ("the sorter is alive but the
    timebase message is missing — see docs/live-spikes.md"). **Start the
    sorter and confirm it is publishing before running check-rig**, not
    after — a FAIL here is not something alhazen can fix.

## Devices this rig doesn't have

Any device left out of the rig YAML's `devices:` block reports OK with "not
configured on this rig" — that's expected, not a check that was skipped. A
rig without a probe today has no `spikes:`/`recording:` block at all.

## If you can't run this on the rig

A `--pulse` run against `backend: simulated` (or a rig with `devices: {}`)
still exercises the config, the monitor registration and the data root, and
is useful for confirming those before you're at the rig. It proves nothing
about the solenoid, the DAQ, SpikeGLX or the sorter — those need this run on
the actual machine, with everything physically connected.
