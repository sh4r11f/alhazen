# API reference

Generated from the docstrings, so it cannot drift from the code the way a
hand-written reference does.

**What is public.** Everything exported from `alhazen` (the names below) and
everything in the modules on this page. Anything else — a leading-underscore
name, a module not listed here — may change without notice. Deprecations warn
for one minor version before removal, naming the version and the replacement
(`alhazen._deprecation`).

## The top-level package

The names an experiment imports directly.

::: alhazen
    options:
      members: true
      show_root_heading: false
      show_source: false
      summary: true

## Running a session

::: alhazen.session.builder
    options:
      members: [build_session, make_input_provider, make_gaze_input_provider]

::: alhazen.session.runner
    options:
      members: [SessionRunner, pause_menu, host_overlay_shapes]

::: alhazen.session.recorder
    options:
      members: [DataRecorder, ordered_trial_columns]

::: alhazen.session.checks

::: alhazen.session.database

## Writing a task

::: alhazen.task.task

::: alhazen.task.plan

::: alhazen.task.reward_policy

::: alhazen.task.phases

## The trial engine

::: alhazen.core.engine

::: alhazen.core.trial

::: alhazen.core.events

::: alhazen.core.commands

::: alhazen.core.clock

::: alhazen.core.rng

## Scheduling trials

::: alhazen.paradigms.base

::: alhazen.paradigms.config

::: alhazen.paradigms.constant

::: alhazen.paradigms.staircase

::: alhazen.paradigms.questplus

::: alhazen.paradigms.adjustment

::: alhazen.paradigms.blocks

## Training curricula

::: alhazen.training.stages

::: alhazen.training.criteria

::: alhazen.training.supervisor

::: alhazen.training.state

## Configuration

::: alhazen.config.models

::: alhazen.config.loader

::: alhazen.config.snapshot

## Display and stimuli

::: alhazen.display.backend

::: alhazen.display.screen

::: alhazen.display.frames

::: alhazen.display.simulated

::: alhazen.stimuli.base

::: alhazen.stimuli.fixation

::: alhazen.stimuli.photodiode

## Scenes

::: alhazen.scenes.loader

::: alhazen.scenes.model

::: alhazen.scenes.render

::: alhazen.scenes.expr

::: alhazen.scenes.rng

## Devices

::: alhazen.devices.eyetracker.protocol

::: alhazen.devices.eyetracker.viewpixx

::: alhazen.devices.eyetracker.messages

::: alhazen.devices.reward

::: alhazen.devices.sync

::: alhazen.devices.response

::: alhazen.devices.recording

::: alhazen.devices.automated

## Data on disk

::: alhazen.data.naming

::: alhazen.data.paths

::: alhazen.data.manifest

::: alhazen.data.participants

## Analysis

::: alhazen.analysis.io.session

::: alhazen.analysis.sync

::: alhazen.analysis.photodiode

::: alhazen.analysis.report

::: alhazen.analysis.results

## The live dashboard

::: alhazen.dashboard.spec

::: alhazen.dashboard.panels

::: alhazen.dashboard.runtime

## Testing helpers

The public fakes an experiment package uses to test its own task.

::: alhazen.testing

## Errors

::: alhazen.errors
