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

## Rehearsing it, away from the rig

Do this once before you do it for real. Every device in the list above has a
simulated backend, and `alhazen sim-sorter` stands in for the one piece that
comes from outside these repositories — so the whole checkout runs, end to
end, on a laptop:

```bash
# terminal 1 — stand in for the real-time sorter
alhazen sim-sorter --address tcp://127.0.0.1:5556
```

```bash
# terminal 2
alhazen check-rig --rig examples/rig-rehearsal.yaml --pulse
```

That prints an `OK` on every line, including `spikes`, for real reasons: the
sorter really is publishing, over a real socket, in the real wire format, and
check-rig really is listening for it.

**Rehearse the failures too**, because those are the lines you will actually
have to read. `--fault` makes the simulated sorter misbehave in the specific
ways the contract names:

| `--fault` | what check-rig says |
|---|---|
| `none` | `OK spikes: ... N units @ 30000 Hz, 0 dropped, lag 15 ms` |
| `no_seq` | `OK`, but `drops undetectable (no seq)` — the sorter sends no sequence numbers, so a dropped message cannot be noticed at all |
| `silent` | `FAIL ... is the real-time sorter running and publishing?` — nothing on that endpoint |
| `announce_once` | `FAIL ... the sorter never re-announced units` — it is running and publishing, and announced its units only at startup |
| `never_units` | same `FAIL` — it never announces units at all |

The last two matter most: `silent` sends you to the sorter process,
`announce_once` sends you to [docs/live-spikes.md](live-spikes.md), and they
are easy to confuse if you have never seen them side by side. `announce_once`
is also the bug a lab writing its own sorter is most likely to ship, because
it works perfectly for whoever watched the sorter start and is invisible to
everyone else.

The lag in the `OK` line tracks how often the sorter publishes coverage
(`--heartbeat-ms`), and it is the number that decides whether a
between-trials decode can finish in time. Compare it against the real
sorter's.

**What a clean rehearsal proves, and what it does not.** It proves the
config, the data root, the device construction, the pulse code paths and the
whole sorted-spike wire contract. It proves *nothing* about your rig: no
valve opened, no TTL reached a recorder, no probe was read, and the
simulated sorter's spikes are Poisson noise with no receptive fields and no
relationship to anything on a screen. Everything in the list above still has
to be run on the actual machine, with everything physically connected.
