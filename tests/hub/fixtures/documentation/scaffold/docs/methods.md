# Methods

This package is the experiment `alhazen new fixation_demo` writes. These methods describe its
code and its parameter file; they report no data.

## Purpose

A minimal gaze-contingent task: the subject looks at a central point and keeps looking at it.
It shows how an alhazen experiment is put together, and it rehearses a rig's display, eye
tracker and data path before a real experiment does.

## Apparatus

Display geometry, refresh rate, eye tracker and data root come from the rig file a session
is started with (`configs/rig-lab.yaml`, `configs/rig-mac.yaml`, or one of alhazen's shared
rigs). Sizes below are in degrees of visual angle (dva); durations are in milliseconds and
are shown for whole display frames.

## Stimuli

One fixation point, a filled white circle [[param:fix_size_dva]] in diameter, at the centre
of a mid-grey screen. Gaze is tested against a circular window of radius [[param:fix_window_dva]]
around the point. The window is never drawn.

## Procedure

Each trial of [[task:fixation-demo]] begins when the point appears (`FIX_ON`). The subject has
up to [[param:acquire_timeout]] to bring their gaze inside the window; the trial ends as
`NO_FIXATION` if they do not. Fixation is acquired on the first sample inside the window
(`FIX_ACQUIRED`), and must then be held for [[param:hold_duration]]. A sample outside the
window, or a lost sample, during the hold ends the trial as `FIX_BREAK`. A trial that holds
to the end is `FIXATED`. The runner waits [[param:iti]] between trials.

Before the first trial in run and test mode the subject reads: "Look at the dot in the
middle of the screen, and keep your eyes on it. Press SPACE to begin." Simulate mode starts
by itself with an automated gaze that fixates.

## Design

One condition (`fixate`), served by the `sequence` scheduler [[param:paradigm.n_per_condition]]
times in run mode. `FIX_BREAK` trials are not completed, so their condition is served again;
`NO_FIXATION` trials are completed without success.

## Measures

Each trial's row holds its outcome, `acquire_latency_s` (time from the point's appearance to
acquisition) and `hold_duration_s` (the hold the trial asked for). The events file holds
`FIX_ON` and `FIX_ACQUIRED` with their flip times.
