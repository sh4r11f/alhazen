# DATAPixx3 reward: wiring and bench test for a WPI SP220i syringe pump

!!! warning "Status: locally implemented, not physically verified"
    Nothing on this page has been tried on hardware. The wiring follows the
    manufacturers' published pinouts. The backend has software regression
    tests using a stand-in for VPixx's library; passing those tests does not
    verify physical pin voltages, pump behavior or liquid delivery. Until
    the applicable items in [What has been verified](#what-has-been-verified)
    are checked on your own bench, treat the reward line as unverified.
    Do not connect it to an animal.

This page covers one setup: a VPixx DATAPixx3 (the box that hosts the
TRACKPixx3 and drives the VIEWPixx) gating a WPI SP220i syringe pump through
the pump's TTL **Gate** input. It needs two wires and no soldering.

Manufacturer electrical specifications, software behavior and outstanding
physical bench checks are described separately.

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

The backend programs the LOW period on the DATAPixx3's own clock (a finite
hardware digital-output schedule), never with a Python sleep. Its record
describes the gate it **commanded**, not evidence that juice came out.

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

## The rig file

The reward line is part of the machine, so it goes in the rig file of the
machine it is wired to, after the cable check and before bench step B2. The
pump values are what you entered on the pump's keypad. The numbers below
are an example, not defaults or a recommended dose; enter your actual values:

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

`alhazen check-rig` without `--pulse` does not schedule a reward, but it
**does write the configured idle level**. It is not a read-only check. Keep
the pump off until the polarity and physical idle voltage have been checked.
Adding `--pulse` explicitly requests one 50 ms reward pulse (bench step B2).
It checks the whole rig and also pulses any configured sync outputs, so keep
other connected equipment ready for that. A human session never opens the
reward line. `--mode simulate`, or a `--simulate` choice including the reward,
stands the reward down; simulating only other devices leaves the real pump live.

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
another; re-measure after any change. alhazen keys each calibration to the
declared pump model, syringe, rate, gate polarity, line and voltage.

## Power-up and shutdown order

The DATAPixx3's documentation does not say what its digital outputs do while
it boots, resets, has USB unplugged, or is switched off. If Digital Out 0 sits
LOW, or in the undefined 0.5–2 V band, in any of those states, a connected and
powered pump may run. Software cannot prevent that. Until bench step B1 shows
otherwise, follow this order every time:

**Start**

1. Pump power **OFF**.
2. DATAPixx3 on; computer on.
3. Run `alhazen check-rig --rig <your rig>` **without** `--pulse`. It
   opens the DATAPixx3, refuses to continue if anything else claims the
   outputs (see below), drives Digital Out 0 HIGH and reads the device's
   register back. A register readback is not evidence of the voltage on the
   pin.
4. With the meter on DC volts between breakout pins 1 and 13: at least 2 V,
   expected about 5 V. A meter shows the settled level only; it says nothing
   about short glitches.
5. Only now switch the pump **ON**. Its display should show it stopped.

**During a session**: alhazen drives only the outputs it owns,
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

## Output conflicts the backend refuses

The DATAPixx3's digital outputs are shared, and a digital-output schedule
writes the low 16 outputs (Digital Out 0–15) together for as long as it plays
(documented: schedule words are 16 bits; the port returns to its resting
value when the schedule ends). So every check covers that whole footprint,
not just the gate. The backend refuses to claim the gate when:

- **Pixel Mode** is on (documented: it drives all 24 outputs from the
  top-left pixel), or **Pixel Mode GB** is on (it drives outputs 8–23, which
  overlap the schedule's 8–15), or **Pixel Mode B** is on. Pixel Mode B
  drives 16–23; it is also refused because independence of those outputs from
  the schedule is not documented. alhazen does not switch these modes off,
  because a recording may rely on them. If this pypixxlib cannot read Pixel
  Mode B (older releases lack `DPxIsDoutPixelModeB`), the gate is refused
  unless the DATAPixx3's firmware revision is below 27, where VPixx documents
  the mode does not exist; it is never assumed off. DATAPixx sync lines on
  outputs 16–23 follow the same rule.
- **Backlight pulse** is enabled (documented: it puts the LCD backlight on
  Digital Out 15, inside the schedule's footprint), whichever output the gate
  is on.
- **Button schedules** are enabled (a button press would start a
  digital-output schedule from the same memory the reward uses).
- A digital-output schedule is already running.
- The reward's schedule memory would overlap the TRACKPixx3's sample buffer
  or camera image, or run past the device's RAM.
- The installed pypixxlib lacks one of the calls the backend uses. That is
  checked before the DATAPixx3 is opened, so nothing is touched.
- **Any DATAPixx sync backend alongside DATAPixx reward**, including outputs
  16–23. The rig validator and runtime ownership check both reject it.

Reading these settings makes one register-cache round trip
(`DPxUpdateRegCache` writes libdpx's cached registers back to the device and
reads them again; pypixxlib has no read-only refresh). No digital-output value
is written by these checks.

Each reward train is written to the DATAPixx3's memory in pieces of at most
16,380 words, the size VPixx's own `DPxWriteDoutBuffer` uses. pypixxlib's
`DPxWriteRam` silently drops anything past 65,535 bytes in one call. The whole
train is then read back and compared word for word. A train starts only if
every word matches; otherwise it is refused and nothing plays. A train longer
than the schedule memory holds (524,288 ms) is refused, never shortened.

With a TRACKPixx3 on the same DATAPixx3, the tracker is connected first; the
session builder and the Measure rig do this. pypixxlib's tracker object opens
the device itself, and the backend refuses to connect a tracker after the
reward has opened it rather than let the device be opened twice (which VPixx
does not document).

A reward that failed after its train started, or whose resting level could not
be read back, is recorded as `REWARD_FAILED` with `"uncertain": true` and the
error text. The trial's `rewarded` column is then left empty rather than
`False`: some or all of the train may have reached the pump. It is never
retried. The live juice plot counts it as a failed delivery and does not add
its volume.

A digital-output gate is never combined with DATAPixx sync, and there is no
override. To use DATAPixx sync with DATAPixx reward on one device, put the gate
on an analog output instead
([Analog output gate](#analog-output-gate-reward-sync-on-one-datapixx3)).
DATAPixx sync without a DATAPixx reward is supported, and so is an NI-DAQ
providing sync alongside a DATAPixx reward. Reading the pump's Run output is
not implemented.

## Analog output gate (reward + sync on one DATAPixx3)

!!! warning "Not physically verified"
    Like the rest of this page, this has software tests only. Nothing here has
    been tried on a DATAPixx3 or a pump.

A digital-output gate shares the Digital Out port with DATAPixx sync lines,
and its schedule rewrites outputs 0–15 together, so the two are refused
together. To have the reward and the recording sync on one DATAPixx3, drive the
pump's Gate from one of the DATAPixx3's **analog outputs** instead. Those are
the DACs, on the separate **Analog I/O** port. The DAC schedule then times the
gate, and the whole Digital Out port stays free for sync lines.

**Wiring.** The Analog I/O port is a female DB25 of its own, next to Digital
Out and looking the same. Use a second male DB25 breakout on the **Analog
I/O** port, not on Digital Out.

| DATAPixx3 Analog I/O DB25 | Pump TTL I/O DE9 |
|---|---|
| DAC0, pin 11 (DAC1 pin 24, DAC2 pin 12, DAC3 pin 25) | pin 4, Gate |
| analog ground, pin 13 (or pin 10) | pin 3, ground |

- Never use pin 23 (+5 VDC). It is a supply that cannot be switched, so the
  pump would run or stop permanently.
- Pin 1 of the Analog I/O port is an analog input (ADC0), not an output.
- Pump pins 7 (Run) and 8 (Trigger) stay unconnected, as before.

The DACs are 16-bit and drive ±10 V at up to ±25 mA, which is far outside
the pump's TTL input. On paper the pump reads LOW at 0–0.5 V (sinking at most
2 mA) and HIGH at 2–5 V. alhazen therefore uses only two levels: **0 V** and
`voltage`. `voltage` must be above 2 V and at most 5 V, and defaults to 5.0.
It never commands a negative level or one above `voltage`. With
`polarity: active_low` the gate rests at `voltage` and goes to 0 V while
dispensing.

**Rig file.**

```yaml
devices:
  reward:
    backend: datapixx
    channel: dac0            # Analog I/O DB25 pin 11
    polarity: active_low     # rests at `voltage`, 0 V runs the pump
    voltage: 5.0             # the HIGH level, (2, 5] V
    pump:
      model: WPI SP220i
      syringe_diameter_mm: 26.59   # example
      rate_ul_per_min: 3000        # example
  sync:
    backend: datapixx
    event_lines:
      STIM_ON: DATAPIXX3/dout2
```

`dac_buffer_address` (default 10,000,000) places the schedule in the
DATAPixx3's memory. It must not overlap the digital-output gate's region
(VPixx's own examples also use 8,000,000), the TRACKPixx3's sample buffer or
its camera image. A calibration measured on a DAC gate counts only for that
channel, polarity, voltage and pump setting.

**What alhazen refuses before writing to the DAC:**

- DAC-ADC loopback is on.
- DAC raw (uncalibrated) mode is on.
- A DAC schedule is already running.
- Another DAC channel is set up for schedule buffering. alhazen never switches
  it off.
- The TRACKPixx3 drives that DAC with eye data, or that cannot be read. The
  `TPxGetAllDACOutput` wrapper in the pypixxlib source VPixx publishes raises
  an error, and with that version the gate is refused.
- The channel's range (read every time) cannot hold 0 V and `voltage`, or a
  level would land outside the pump's bands.
- The pypixxlib in use lacks a call the gate needs.

alhazen computes each sample's 16-bit code itself, using pypixxlib's own
formula. It writes the train in pieces, reads it back word for word, and only
then starts a finite DAC schedule. The first and last samples are at rest.
VPixx documents no return to a resting value when a DAC schedule ends or is
stopped. After every train, and after any stop, alhazen therefore writes the
rest level and reads it back, allowing one code of tolerance. As with the
digital gate, a failure after the start is recorded as uncertain and never
retried. The tracker connects first.

**Power-up.** VPixx does not document the DAC output at power-up, reset,
USB loss or power-off. A DAC sitting at code 0 outputs **0 V, which runs the
pump on an active-low gate**. Follow the same order as above: pump off until
`alhazen check-rig` (without `--pulse`) has set the rest level and you have
measured it at the pump connector; pump off before the DATAPixx3 is switched
off or unplugged; keep PWR UP: STOP.

**Bench steps.** Use the same water-only steps B0–B7, with the meter or scope
between Analog I/O pin 11 and pin 13 instead of Digital Out pins 1 and 13. Add
these checks:

- the level at the pump's pins 4–3 under load, at rest and while active;
- the DAC pin's level at power-up and after USB loss (B1);
- the level after a train ends, and after a train is killed mid-way (B7);
- with sync lines configured: each sync line pulses cleanly during a reward,
  and the gate does not move during a sync pulse (B6).

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

**B5. Latency.** Measure the time from the gate's falling edge to the pump's
Run indication, and from its rising edge to that indication ending. A
bench-only third wire from pump pin 7 (Run indicator, HIGH while running) to
the scope or analyser, together with Digital Out 0, shows the two electrical
signals. This measures the pump's indication, not actual motor movement or
liquid arriving at the outlet. Check outlet delivery separately with the
actual syringe and tubing; do not report the Run indication as liquid latency.

**B6. Shared tracker and other outputs.** With TRACKPixx3 recording, monitor
Digital Out 0 and the other DOUT lines with the analyser during rewards.
Check the gate waveform, that unrelated lines stay at their prior levels,
and that gaze recording continues. Do not configure DATAPixx sync with a
digital-output gate: the backend rejects that combination (an analog-output
gate may share the device with sync; see its own bench checks). Investigating upper-output
independence for a future implementation requires a separate reviewed bench
diagnostic, not an override of this refusal.

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
| Unrelated outputs stable and tracker continues during reward | bench B6 | **not verified** |
| DATAPixx sync together with a digital-output DATAPixx reward | validation and runtime ownership | **not supported; rejected** |
| DATAPixx sync together with an analog-output (DAC) DATAPixx reward | software tests with a stand-in SDK, including a full session | **software tested; not hardware verified** |
| DAC pins: Analog I/O DB25 DAC0 pin 11, DAC1 24, DAC2 12, DAC3 25, ground 10 and 13, +5 VDC on pin 23; ±10 V, ±25 mA, 16 bits | VPixx DATAPixx3 documentation | documented |
| DAC level at power-up, reset, USB loss, power off; after a schedule ends or is stopped; loaded levels at the pump | bench B1, B7 on the Analog I/O pins | **not verified** |
| Failure drills | bench B7 | **not verified** |
| alhazen DATAPixx backend (refusals, timing, uncertain-state handling) | software regression tests with a stand-in SDK | **software tested; not hardware verified** |
