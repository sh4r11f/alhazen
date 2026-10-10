# DATAPixx3 reward: wiring and bench test for a WPI SP220i syringe pump

!!! warning "Status: proposed, not physically verified"
    Nothing on this page has been tried on hardware. The wiring follows the
    manufacturers' published pinouts, and alhazen's DATAPixx reward backend has
    only been tested in software, against a stand-in for VPixx's library. A
    passing test suite says nothing about what the pump does. Until every item
    in [What has been verified](#what-has-been-verified) is ticked on your own
    bench, treat this rig's reward line as unverified. Do not connect it to an
    animal.

This page covers one setup: a VPixx DATAPixx3 (the box that hosts the
TRACKPixx3 and drives the VIEWPixx) gating a WPI SP220i syringe pump through
the pump's TTL **Gate** input. It needs two wires and no soldering.

## How the pump is driven

The SP220i has its own motor driver. The DATAPixx3 only tells it when to run:

| Gate input (pump DE9 pin 4) | Pump |
|---|---|
| HIGH (2–5 V) | stopped |
| HIGH → LOW | starts, and keeps running while LOW |
| LOW → HIGH | stops |

So the line **idles HIGH**, goes **LOW for the reward**, and **ends HIGH**.
This is the opposite of the NI-DAQ solenoid line (idle 0 V, open at 5 V); do
not copy a solenoid setting to this rig.

How much liquid a reward gives is set by the pump, not by alhazen: the syringe
diameter and the rate entered on the pump, times how long the gate is LOW.
alhazen cannot read either pump setting, so the rig file declares them and you
must keep them true. The DATAPixx3 times the LOW period on its own clock
(a hardware digital-output schedule), never with a Python sleep. What alhazen
records is the gate it **commanded**. It does not know that juice came out.

## What you need

- A **male DB25** screw-terminal breakout (the DATAPixx3 digital output port
  is female).
- A **male DE9** screw-terminal breakout (the pump's TTL I/O port is female;
  check yours).
- Two lengths of insulated hookup wire, about 22–24 AWG, as long as the bench
  needs. Two colours help: one for the signal, one for ground.
- A multimeter with a continuity setting.
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
numbering is mirrored. The rig file then names this line `dout0`.

Why these levels are within spec (from the datasheets, not yet measured):
the DATAPixx3 drives 5 V through 25 Ω and can sink 12 mA; the pump reads
0–0.5 V as LOW and sinks at most 2 mA, and reads 2–5 V as HIGH. A LOW from the
DATAPixx3 therefore sits near 0.05 V, and its HIGH at about 5 V.

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

## The rig file

The reward line is part of the machine, so it goes in the rig file of the
machine it is wired to, after the cable check and before bench step B2. The
pump values are what you entered on the pump's keypad:

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

`alhazen check-rig` validates this without moving the pump; `--pulse` runs
one short reward and is a bench step (B2), not a routine check. A human
session never opens the reward line whatever the rig file says.
`--mode simulate`, or a `--simulate` choice that includes the reward, stands
it down (deliveries are logged, the DATAPixx3's outputs are not touched). A
`--simulate` choice that names only other devices, the tracker say, leaves the
real pump live.

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
  B4 below finds out. Until then, set a target no larger than the most you
  would accept the subject receiving in one session.
- **PWR UP: STOP.** With RUN selected and no target volume, the pump resumes
  running when its power comes back after an interruption.

alhazen records the declared syringe and rate with every session and with each
reward calibration. A calibration measured at one rate or syringe does not
apply to another; re-measure after any change.

## Power-up and shutdown order

The DATAPixx3's documentation does not say what its digital outputs do while
it boots, while USB is unplugged, or while it is switched off. If Digital Out
0 sits LOW in any of those states, a connected and powered pump will run.
Software cannot prevent that. Until bench step B1 shows otherwise, follow this
order every time:

**Start**

1. Pump power **OFF**.
2. DATAPixx3 on; computer on.
3. Run `alhazen check-rig --rig <your rig>` **without** `--pulse`. It opens the
   DATAPixx3, refuses to continue if anything else claims the line (see
   below), drives Digital Out 0 HIGH and reads it back.
4. With the meter on DC volts between breakout pins 1 and 13: about 5 V.
5. Only now switch the pump **ON**. Its display should show it stopped.

**During a session**, alhazen drives only the gate bit, re-asserts it HIGH after
every reward, and stops with an error instead of guessing when it cannot
confirm the line is HIGH. It never repeats a reward whose outcome it could not
confirm.

**Stop**

1. End the session.
2. Pump **OFF** (or press its run/stop key, then switch it off).
3. Quit alhazen, then shut down the computer and the DATAPixx3.

Never unplug the DATAPixx3's USB or power while the pump is on.

**To stop the pump at once:** its run/stop key, its power switch, or unplug the
DE9. An unplugged Gate input most likely reads HIGH (the pump sinks current
from it when LOW, which suggests a pull-up), so the pump should stop, but this
is unverified: bench step B1 checks it.

What software cannot cover: a power cut, the DATAPixx3 resetting, USB failing
or the computer freezing mid-reward. In each, the pump's own settings (rate,
target volume, PWR UP STOP) and the order above are the protection. A fail-safe
alternative is an inverting stage (an opto-isolator or open-collector
transistor) so that a LOW or unpowered DATAPixx3 output means "stopped"; it
needs more than two wires, flips the polarity in the rig file, and is not
proposed here.

## Things alhazen refuses before touching the line

The DATAPixx3's digital outputs are shared. alhazen checks, and refuses to
start a reward line, when:

- **Pixel Mode** is on (it drives all 24 outputs from the top-left pixel), or
  **Pixel Mode GB** (outputs 8–23) or **Pixel Mode B** (16–23) covers the
  reward line. These are set in LabMaestro (Digital IO → Output Mode) and
  persist on the device. alhazen does not switch them off, because a recording
  may rely on them.
- **Button schedules** are enabled (a button press would start a digital-output
  schedule from the same memory the reward uses).
- **Backlight pulse** is enabled and the reward line is Digital Out 15.
- A digital-output schedule is already running.
- The reward's schedule memory would overlap the TRACKPixx3's sample buffer.

A digital-output schedule writes the low 16 outputs (0–15) together for as long
as it plays. Sync pulses on those outputs would be overwritten during a reward,
so alhazen refuses DATAPixx sync lines below 16 while the reward uses the
DATAPixx3. Whether outputs 16–23 are left alone by a schedule is not documented
and is checked in bench step B6.

## Bench test

Rules for every step: **no animal, no needle, no person's mouth.** Water only,
into a beaker on a scale. Wear eye protection when the line is under pressure.
Keep a hand near the pump's run/stop key. Record every number in the rig's
notes with the date, the pump settings and the alhazen version.

**B0. Cable check.** The continuity check above, passed and written down.

**B1. Idle levels, pump unplugged.** Meter between breakout pins 1 and 13,
pump cable disconnected. Record the voltage:

1. DATAPixx3 off.
2. During DATAPixx3 power-up, and once it has booted, before alhazen runs.
3. After `alhazen check-rig` without `--pulse` (expect about 5 V).
4. With the DATAPixx3's USB unplugged, then replugged.
5. After alhazen exits normally, and after it is killed (end the process).

Any LOW reading (below about 2 V) in steps 1, 2, 4 or 5 is a state in which a
powered pump would run; the start/stop order above is then mandatory, not just
advisable. Then, with the pump connected and powered but alhazen idle,
confirm: no motion for 60 s; unplugging the DE9 keeps it stopped.

**B2. One pulse.** Pump primed, outlet into the beaker. Run
`alhazen check-rig --rig <your rig> --pulse`. The pump should run once,
briefly, and stop. If it keeps running, stop it with its key and do not
continue.

**B3. Dose versus gate time.** Deliver a fixed train (for example 20 rewards
of 200 ms) through the Measure rig's "Juice per pulse" measurement. Weigh the
water. Compare µL per reward with rate × time. Repeat at two other widths.
Short pulses may deliver less than rate × time (the stepper ramps), so
calibrate at the widths you will use, never extrapolate.

**B4. Repeated gates and the target volume.** Set a small target volume and
deliver rewards until it is reached and beyond. Record whether each gate
resumes towards the target, whether the pump ignores gates after it, and what
restarts it. Decide from that whether to run with a target volume as a session
cap.

**B5. Latency.** Measure the time from the gate's falling edge to the pump
starting, and from its rising edge to stopping. A bench-only third wire from
pump pin 7 (Run indicator, HIGH while running) to an oscilloscope or logic
analyser together with Digital Out 0 shows both. Do not guess this number;
alhazen does not.

**B6. Shared outputs.** With the TRACKPixx3 recording and any sync lines
connected to a logic analyser, run rewards and sync pulses together. Check no
sync pulse is lost or stretched during a reward, and that no output other than
Digital Out 0 changes during a reward schedule.

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
| DATAPixx3 Digital Out is DB25 female; DOUT0 on pin 1, GND on pin 13; 5 V through 25 Ω, 15 mA source, 12 mA sink | VPixx DATAPixx3 documentation | documented |
| SP200-series TTL pinout: Gate pin 4, ground pin 3, Trigger pin 8, Run indicator pin 7; LOW 0–0.5 V at most 2 mA, HIGH 2–5 V | WPI SP200 Series manual (041516), p. 14 | documented |
| Pump DE9 is female | photo of the lab's pump | observed, not measured |
| Port returns to its static value when a digital-output schedule ends | VPixx Datapixx button-schedule demo | documented |
| Schedule words are 16 bits (outputs 0–15) | pypixxlib `DPxWriteDoutBuffer` | documented |
| Gate behaviour, latency, dose per gate, repeated gates, target volume | bench B2–B5 | **not verified** |
| Digital Out 0 level at power-up, USB loss, power off | bench B1 | **not verified** |
| Outputs 16–23 untouched by a schedule; sync and reward together | bench B6 | **not verified** |
| Failure drills | bench B7 | **not verified** |
| alhazen DATAPixx backend | unit tests against a stand-in library | software only |
