# DATAPixx3 reward: wiring and bench test for a WPI SP220i syringe pump

!!! warning "Status: proposed. Not physically verified, not yet software-verified"
    Nothing on this page has been tried on hardware. The wiring follows the
    manufacturers' published pinouts. alhazen's DATAPixx reward backend is
    still being written; its software verification is pending the final
    integrated test suite, and no test result is claimed here. The rig-file
    names and commands below are provisional and will be reconciled with the
    backend when it lands. Until every item in
    [What has been verified](#what-has-been-verified) is done on your own
    bench, treat this rig's reward line as unverified. Do not connect it to an
    animal.

This page covers one setup: a VPixx DATAPixx3 (the box that hosts the
TRACKPixx3 and drives the VIEWPixx) gating a WPI SP220i syringe pump through
the pump's TTL **Gate** input. It needs two wires and no soldering.

Each section separates what the manufacturers **document** from what alhazen's
backend is **planned** to do.

## How the pump is driven

Documented (WPI SP200 Series manual, rev. 041516, p. 14):

| Gate input (pump DE9 pin 4) | Pump |
|---|---|
| HIGH (2–5 V) | stopped |
| HIGH → LOW | starts, and keeps running while LOW |
| LOW → HIGH | stops |
| LOW is 0–0.5 V | |
| 0.5–2 V | not defined by the manual: treat as unsafe, neither run nor stop is guaranteed |

So the line **idles HIGH**, goes **LOW for the reward**, and **ends HIGH**.
This is the opposite of the NI-DAQ solenoid line (idle 0 V, open at 5 V); do
not copy a solenoid setting to this rig.

How much liquid a reward gives is set by the pump, not by alhazen: the syringe
diameter and the rate entered on the pump, times how long the gate is LOW.
alhazen cannot read either pump setting, so the rig file declares them and you
must keep them true.

Planned: the DATAPixx3 times the LOW period on its own clock (a hardware
digital-output schedule), never with a Python sleep, and alhazen records the
gate it **commanded**. A commanded gate is not evidence that juice came out.

## What you need

- A **male DB25** screw-terminal breakout (the DATAPixx3 digital output port
  is female).
- A **male DE9** screw-terminal breakout (the pump's TTL I/O port is female
  in the lab's photo; check yours).
- Two lengths of insulated hookup wire, about 22–24 AWG, as long as the bench
  needs. Two colours help: one for the signal, one for ground.
- A multimeter with DC volts and continuity.
- An oscilloscope or a logic analyser that tolerates 5 V signals, for the
  transient and pulse-width checks (a multimeter cannot see them).
- For the bench test: a syringe with **no needle**, tubing, a beaker, water and
  a scale that reads to 1 mg (1 mg of water is 1 µL).

Do **not** use a ready-made cable, a DB25-to-DB9 adapter or a serial cable.
They connect pins by their own scheme, and one of them could put a
digital-output line on the pump's Trigger pin (pin 8, which toggles the pump on
every falling edge) or on its reverse-direction pin (pin 9).

## Find the right ports

Both devices carry other connectors of the same shape. Read the labels.

- **DATAPixx3, rear panel:** three DB25 sockets. Use the one labelled
  **Digital Out**. The Digital In socket and the Analog I/O socket look the same
  (Analog I/O pin 1 is an analog input, not a digital output). The VIEWPixx has
  its own I/O ports; the wiring here goes to the DATAPixx3's.
- **SP220i, rear panel:** the DE9 labelled **TTL I/O**. The two sockets
  labelled RS232 IN and OUT are modular (telephone-style) jacks and are not
  used.

## Wiring: two wires

| Wire | DATAPixx3 Digital Out (male DB25 breakout) | SP220i TTL I/O (male DE9 breakout) |
|---|---|---|
| Signal | pin **1** (Digital Out 0) | pin **4** (Gate) |
| Ground | pin **13** (GND) | pin **3** (ground) |

Leave every other pin on both breakouts unconnected. In particular, never
connect:

- pump pin **8** (Trigger: each falling edge starts or stops the pump, so a
  missed edge leaves it running);
- pump pin **9** (reverse direction on infuse/withdraw models);
- pump pins 1, 2, 5, 6 and 7 (outputs or undefined).

Use the pin numbers printed on each breakout's own terminal legend. Do not
count pins on the connector face: viewed from the front and from the back the
numbering is mirrored.

### Electrical compatibility

Documented ratings: the DATAPixx3's digital outputs drive 5 V through a 25 Ω
series resistor and are rated to source 15 mA and sink 12 mA. The pump's input
reads 0–0.5 V as LOW, with at most 2 mA flowing out of the input that the
driving output must sink, and 2–5 V as HIGH.

So, on paper, the DATAPixx3 output can sink the pump input's LOW current with
margin (12 mA rated against at most 2 mA). That is rated compatibility, not a
measured level: the LOW voltage at the pump is the output stage's own
low-level voltage plus the drop across the series resistor, and VPixx does not
publish the output stage's low-level voltage. Measure it (bench step B1): with
the pump connected and the line driven LOW, the voltage between DE9 pins 4 and
3 must be at most 0.5 V; driven HIGH, at least 2 V.

## Check the cable before it touches either device

With both breakouts **unplugged from the devices**, set the meter to
continuity and check:

1. DB25 pin 1 ↔ DE9 pin 4: beeps.
2. DB25 pin 13 ↔ DE9 pin 3: beeps.
3. DB25 pin 1 ↔ DB25 pin 13: silent (no short between signal and ground).
4. DE9 pin 4 against pins 3, 5, 8 and 9: silent.
5. DB25 pin 1 against its neighbours (pins 2, 14 and 25): silent.
6. Tug each wire at its terminal; re-check 1 and 2.

Write down that the check passed and the date.

## The rig file (provisional)

The reward line is part of the machine, so it goes in the rig file of the
machine it is wired to, after the cable check and before bench step B2. The
pump values are what you entered on the pump's keypad. Field names follow the
backend's draft and may change:

```yaml
devices:
  reward:
    backend: datapixx
    device: DATAPIXX3
    channel: dout0          # Digital Out 0, DB25 pin 1
    polarity: active_low    # SP220i Gate: LOW runs the pump
    pump:
      model: WPI SP220i
      syringe_diameter_mm: 26.59   # example: the value the pump shows
      rate_ul_per_min: 3000        # example: the value the pump shows
```

Planned behaviour, to be confirmed against the integrated backend:
`alhazen check-rig` validates this without moving the pump, and `--pulse`
runs one short reward, which is a bench step (B2), not a routine check. A human
session never opens the reward line whatever the rig file says (this is
existing alhazen behaviour). `--mode simulate`, or a `--simulate` choice that
includes the reward, stands it down so the DATAPixx3's outputs are not
touched; a `--simulate` choice that names only other devices, the tracker say,
leaves the real pump live.

## Pump settings that are part of safety

Set these on the pump's keypad before it is ever wired:

- **Syringe:** pick it from the pump's table, or enter the measured inner
  diameter. The rig file's `syringe_diameter_mm` must match.
- **Mode:** infusion.
- **Rate:** the slowest rate that gives a reward in a reasonable time. A stuck
  LOW line runs the pump at this rate until something stops it; with a 60 mL
  syringe at the pump's maximum (about 70 mL/min) that is over a millilitre a
  second. The rig file's `rate_ul_per_min` must match.
- **Volume:** a target volume makes the pump stop by itself when it is
  reached, so it can act as a physical cap on a whole session. The manual
  describes stop/run as a pause that resumes towards the target, so gate
  pulses probably add up towards one total rather than restart each time; what
  happens to gates after the target is reached is not documented. Bench step
  B4 finds out. Until then, set a target no larger than the most you would
  accept the subject receiving in one session.
- **PWR UP: STOP.** With RUN selected and no target volume, the pump resumes
  running when its power comes back after an interruption (documented).

A reward calibration measured at one rate or syringe does not apply to
another; re-measure after any change. (Planned: alhazen keys each calibration
to the declared syringe and rate.)

## Power-up and shutdown order

The DATAPixx3's documentation does not say what its digital outputs do while
it boots, resets, has USB unplugged, or is switched off. If Digital Out 0 sits
LOW, or in the undefined 0.5–2 V band, in any of those states, a connected and
powered pump may run. Software cannot prevent that. Until bench step B1 shows
otherwise, follow this order every time:

**Start**

1. Pump power **OFF**.
2. DATAPixx3 on; computer on.
3. Run `alhazen check-rig --rig <your rig>` **without** `--pulse`. Planned: it
   opens the DATAPixx3, refuses to continue if anything else claims the
   outputs (see below), drives Digital Out 0 HIGH and reads the device's
   register back. A register readback is not evidence of the voltage on the
   pin.
4. With the meter on DC volts between breakout pins 1 and 13: at least 2 V,
   expected about 5 V. A meter shows the settled level only; it says nothing
   about short glitches.
5. Only now switch the pump **ON**. Its display should show it stopped.

**During a session** (planned): alhazen drives only the outputs it owns,
re-asserts the gate HIGH after every reward, and stops with an error instead
of guessing when it cannot confirm the line state. It never repeats a reward
whose outcome it could not confirm.

**Stop**

1. End the session.
2. Pump **OFF** at its power switch.
3. Quit alhazen, then shut down the computer and the DATAPixx3.

Never unplug the DATAPixx3's USB or power while the pump is on.

**To stop the pump at once:** switch the pump **off at its power switch**.
Its run/stop key also stops it if the key works in your pump's current state;
use it as a second option, not instead of the power switch. Do not rely on
unplugging the DE9 or a wire: whether a disconnected Gate input stops the
motor is not documented, and it is tested only as a controlled water-only
bench step (B1).

What software cannot cover: a power cut, the DATAPixx3 resetting, USB failing
or the computer freezing mid-reward. In each, the pump's own settings (rate,
target volume, PWR UP STOP) and the order above are the protection. A
different interface (a hardware interlock, a watchdog, or an isolated
interface between the two devices) could change that, but only as a real,
specified design with a failure-state analysis and its own bench tests. None
is proposed here.

## Things alhazen is planned to refuse before touching the outputs

The DATAPixx3's digital outputs are shared, and a digital-output schedule
writes the low 16 outputs (Digital Out 0–15) together for as long as it plays
(documented: schedule words are 16 bits; the port returns to its resting
value when the schedule ends). So every check covers that whole footprint,
not just the gate. Planned refusals, before any write:

- **Pixel Mode** is on (documented: it drives all 24 outputs from the
  top-left pixel), or **Pixel Mode GB** is on (it drives outputs 8–23, which
  overlap the schedule's 8–15). Pixel Mode B drives 16–23 and does not overlap
  0–15. These are set in LabMaestro (Digital IO → Output Mode) and persist on
  the device. alhazen does not switch them off, because a recording may rely
  on them.
- **Backlight pulse** is enabled (documented: it puts the LCD backlight on
  Digital Out 15, inside the schedule's footprint), whichever output the gate
  is on.
- **Button schedules** are enabled (a button press would start a
  digital-output schedule from the same memory the reward uses).
- A digital-output schedule is already running.
- The reward's schedule memory would overlap the TRACKPixx3's sample buffer.
- A sync line on Digital Out 0–15 is configured alongside the DATAPixx
  reward, since the schedule would overwrite it during every reward.

Sync on Digital Out 16–23 next to the reward is **not verified**: whether a
schedule leaves those outputs untouched is not documented. Do not treat it as
supported until bench step B6 shows it on your device.

## Bench test

Rules for every step: **no animal, no needle, no person's mouth.** Water only,
into a beaker on a scale. Wear eye protection when the line is under pressure.
Keep a hand on the pump's power switch. Record every number in the rig's notes
with the date, the pump settings, the instrument used and the alhazen version.

Classify every voltage reading by the pump's documented bands: **LOW**
0–0.5 V, **HIGH** 2–5 V, **undefined** 0.5–2 V. An undefined reading is not
safe in either direction.

**B0. Cable check.** The continuity check above, passed and written down.

**B1. Idle levels and transients, pump unplugged first.** Meter between
breakout pins 1 and 13, and a scope or logic analyser on the same pins for the
transitions, pump cable disconnected. Record the level in each state, and any
glitch the scope or analyser catches during each transition:

1. DATAPixx3 off.
2. DATAPixx3 power-up, and once it has booted, before alhazen runs.
3. After `alhazen check-rig` without `--pulse` (expect HIGH, about 5 V).
4. USB unplugged, then replugged.
5. alhazen exiting normally, and alhazen killed (end the process).

A LOW or undefined level, or a LOW-going glitch, in steps 1, 2, 4 or 5 is a
state in which a powered pump may run: the start/stop order above is then
mandatory. Then connect the pump, power it with alhazen idle, and confirm:
the HIGH level at DE9 pins 4–3 under load is at least 2 V; no motion for 60 s.
Drive the line LOW once from a controlled test with water only and confirm the
loaded LOW level is at most 0.5 V. Only as a controlled water-only test,
unplug the DE9 while the pump is idle and record whether it stays stopped;
this is a measurement of one pump, not an emergency procedure.

**B2. One pulse.** Pump primed, outlet into the beaker. Run
`alhazen check-rig --rig <your rig> --pulse`. The pump should run once,
briefly, and stop. If it keeps running, switch it off and do not continue.

**B3. Dose versus gate time.** Deliver a fixed train (for example 20 rewards
of 200 ms) through the Measure rig's "Juice per pulse" measurement. Measure
the gate's actual LOW width with the scope or analyser. Weigh the water.
Compare µL per reward with rate × time. Repeat at two other widths. Short
pulses may deliver less than rate × time (the stepper ramps), so calibrate at
the widths you will use, never extrapolate.

**B4. Repeated gates and the target volume.** Set a small target volume and
deliver rewards until it is reached and beyond. Record whether each gate
resumes towards the target, whether the pump ignores gates after it, and what
restarts it. Decide from that whether to run with a target volume as a session
cap.

**B5. Latency.** Measure the time from the gate's falling edge to the pump
starting, and from its rising edge to stopping. A bench-only third wire from
pump pin 7 (Run indicator, HIGH while running) to the scope or analyser,
together with Digital Out 0, shows both. Do not guess this number.

**B6. Shared outputs.** With the TRACKPixx3 recording and any sync lines
connected to the logic analyser, run rewards and sync pulses together. Check
that no output other than Digital Out 0 changes during a reward schedule, and
whether outputs 16–23 are left untouched; until they are shown to be, sync
next to the reward stays unsupported.

**B7. Failure drills, water only.**

1. Kill the alhazen process during a long reward (for example 2 s). Record
   whether the pump stops when the schedule ends.
2. Unplug the DATAPixx3's USB during a long reward. Record what the pump does.
3. With the pump running from its keypad, cut its power and restore it:
   it must stay stopped (PWR UP STOP).

Record what happened in each, including "the pump kept running".

## What has been verified

| Item | Source | Status |
|---|---|---|
| DATAPixx3 Digital Out is DB25 female; DOUT0 on pin 1, GND on pin 13; 5 V through 25 Ω, rated 15 mA source, 12 mA sink | VPixx DATAPixx3 documentation | documented |
| SP200-series TTL pinout: Gate pin 4, ground pin 3, Trigger pin 8, Run indicator pin 7; LOW 0–0.5 V with at most 2 mA to sink, HIGH 2–5 V | WPI SP200 Series manual (041516), p. 14 | documented |
| Pump DE9 is female | photo of the lab's pump | observed, not measured |
| Port returns to its resting value when a digital-output schedule ends | VPixx Datapixx button-schedule demo | documented |
| Schedule words are 16 bits (outputs 0–15) | pypixxlib `DPxWriteDoutBuffer` | documented |
| Loaded LOW ≤ 0.5 V and HIGH ≥ 2 V at the pump input | bench B1 | **not verified** |
| Digital Out 0 level and glitches at power-up, reset, USB loss, power off | bench B1 (scope or analyser) | **not verified** |
| Disconnected Gate input behaviour | bench B1 (controlled, water only) | **not verified** |
| Gate behaviour, latency, dose per gate, repeated gates, target volume | bench B2–B5 | **not verified** |
| Outputs 16–23 untouched by a schedule; sync and reward together | bench B6 | **not verified** |
| Failure drills | bench B7 | **not verified** |
| alhazen DATAPixx backend (refusals, timing, uncertain-state handling) | final integrated test suite | **planned; software verification pending** |
