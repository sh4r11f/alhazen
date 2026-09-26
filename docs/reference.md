# API reference

Generated from the docstrings, so it cannot drift from the code the way a
hand-written reference does.

**What is public.** Exactly the names on this page: everything exported from
`alhazen` (the first section below), and the members each module's entry
lists. A listed class comes with its methods and attributes, except those
starting with `_`. Nothing else is public, even without a leading underscore:

- a name that a listed module defines but that its entry does not list — a
  formatting helper, a tuning constant, a real device backend's class — is
  internal. It may change or disappear in any release, without a
  deprecation;
- so is everything in a module that is not on this page;
- a subpackage that re-exports a listed name for a shorter import
  (`from alhazen.scenes import load_scene`) hands out the same public object,
  but a subpackage's `__all__` does not by itself make a name public. It
  holds only names this page lists for that subpackage or its modules; an
  internal name the subpackage imports stays importable from it, but is not
  exported.

The lists are what experiments built on alhazen actually import, what
`examples/`, the `alhazen new` template and the snippets in these docs use,
and what the guides tell a task author to call, subclass or implement.
Everything else stays importable, at the importer's own risk.
`tests/unit/test_docs_snippets.py` fails when a listed name no longer exists,
so a public name cannot be renamed or removed without this page — and the
version number — noticing; it also fails when a subpackage's `__all__`
exports a name this page does not list. A deprecated name keeps working
until the next major version removes it, and until then it warns, naming
that version and the replacement (`alhazen._deprecation`; the policy is §4 of
[Versioning and releases](versioning.md)).

## The top-level package

The names an experiment imports directly.

::: alhazen
    options:
      members: [ABORTED, DROPPED_FRAMES, PAUSED, TRIAL_RECORD_COLUMNS, AlhazenError,
        build_session, BuildTrial, CircleRegion, Condition, ConfigError, Curriculum,
        DataError, LiveMonitorConfig, DatabaseConfig, LiveMonitorPanel, LiveMonitorSpec,
        DevicesConfig, DisplayConfig, DisplayError, DeviceSample, Duration, Event,
        EventBus, EventSchema, ExperimentDatabase, EyeTrackerConfig, FrameQAConfig,
        FrameQAError, InputFrame, Model, MonitorConfig, Outcome, outcomes, OutcomeSet,
        Phase, PhaseAction, PhotodiodeConfig, QuitRequested, Ramp, RewardError,
        RewardHwConfig, RewardPolicy, RewardPulses, RewardRequestError, RigConfig,
        SchedulerConfig, Screen, SessionConfig, SessionError, SessionInfo,
        SessionRunner, SimpleSequence, Stage, StageCriteria, SyncError, SyncHwConfig,
        Task, TrackerError, TrialContext, TrialEngine, TrialPlan, TrialResult,
        TrialSetup, TrialSource, __version__]
      show_root_heading: false
      show_source: false
      summary: true

## Running a session

::: alhazen.session.builder
    options:
      members: [build_session, make_input_provider, make_gaze_input_provider,
        validate_event_names]

::: alhazen.session.runner
    options:
      members: [SessionRunner, pause_menu, host_overlay_shapes]

::: alhazen.session.pause
    options:
      members: [build_pause_menu, run_pause_menu, PauseMenu]

::: alhazen.session.recorder
    options:
      members: [DataRecorder, ordered_trial_columns]

::: alhazen.session.checks
    options:
      members: [CheckResult, check_rig]

::: alhazen.session.checkout
    options:
      members: [CheckoutRecord, build_record, read_record, differences]

::: alhazen.session.database
    options:
      members: [DeviceSample, ExperimentDatabase]

## Starting a session in a mode

The six modes and the hooks a task fills in for them; [docs/modes.md](modes.md)
explains each.

::: alhazen.cli.modes
    options:
      members: [run_experiment]

::: alhazen.modes
    options:
      members: [Mode]

::: alhazen.modes.session
    options:
      members: [ModeSession, build_mode_session, rig_for_mode]

::: alhazen.modes.rehearsal
    options:
      members: [rehearsal_root]

::: alhazen.modes.simulation
    options:
      members: [Simulation]

::: alhazen.modes.demo
    options:
      members: [DemoSetup, DemoView, DemoControl, DemoState, run_demo, RESERVED_KEYS,
        BUILT_IN_KEYS, CAPTION_Y_FRACTION, KEYS_X_FRACTION, KEYS_Y_FRACTION,
        KEYS_HEIGHT_SCALE]

::: alhazen.modes.movie
    options:
      members: [MovieSetup, MovieClip, run_movie, record_clip, scale_frame, to_uint8]

## Writing a task

::: alhazen.task.task
    options:
      members: [Task]

::: alhazen.task.plan
    options:
      members: [TrialSetup, TrialPlan, BuildTrial]

::: alhazen.task.reward_policy
    options:
      members: [RewardPolicy]

::: alhazen.task.phases
    options:
      members: [AcquireFixation, AdjustmentLoop, Blank, Feedback, FrameSequence,
        HoldFixation, LandingCheck, LandingSample, ResponseWindow, StimulusResponse,
        TrialFeedback]

::: alhazen.task.phases.simple
    options:
      members: [SUCCESS_COLOR, FAILURE_COLOR]

::: alhazen.task.live
    options:
      members: [LiveWiring, LiveAnalysis]

## The trial engine

::: alhazen.core.engine
    options:
      members: [TrialEngine, TrialResult, QuitRequested]

::: alhazen.core.trial
    options:
      members: [Outcome, OutcomeSet, outcomes, PAUSED, ABORTED, DROPPED_FRAMES,
        TRIAL_RECORD_COLUMNS, NO_FAULT, FAULT_DROPPED_FRAMES, FAULT_TRACKER_STOPPED,
        HealthFault, lost_to_fault, CircleRegion, InputFrame, PhaseAction, Phase,
        TrialContext]

::: alhazen.core.events
    options:
      members: [Event, EventBus, EventSchema, RESERVED_EVENTS]

::: alhazen.core.commands
    options:
      members: [Command, CommandSource, DEFAULT_KEYMAP]

::: alhazen.core.clock
    options:
      members: [Clock, MonotonicClock]

::: alhazen.core.rng
    options:
      members: [STREAMS, spawn_streams]

## Scheduling trials

::: alhazen.paradigms.base
    options:
      members: [Condition, TrialSource, SimpleSequence]

::: alhazen.paradigms.config
    options:
      members: [SchedulerConfig, StaircaseConfig, QuestConfig, BlockConfig]

::: alhazen.paradigms.constant
    options:
      members: [ConstantStimuli]

::: alhazen.paradigms.staircase
    options:
      members: [UpDownStaircase, InterleavedStaircases]

::: alhazen.paradigms.questplus
    options:
      members: [QuestPlus]

::: alhazen.paradigms.adjustment
    options:
      members: [AdjustmentTrials]

::: alhazen.paradigms.blocks
    options:
      members: [BlockPlan]

## Training curricula

::: alhazen.training.stages
    options:
      members: [Ramp, StageCriteria, Stage, Curriculum]

::: alhazen.training.criteria
    options:
      members: [register_metric, completed_rate, success_rate, mean_rt_ms]

## Configuration

::: alhazen.config.models
    options:
      members: [Model, Duration, MonitorConfig, FrameQAConfig, PhotodiodeConfig,
        DisplayConfig, LiveMonitorConfig, DatabaseConfig, SELF_DRIVEN_CALIBRATION_TYPES,
        EyeTrackerConfig, RewardHwConfig, SyncHwConfig, RewardPulses, RecordingConfig,
        SpikeSourceConfig, DevicesConfig, RigConfig, SessionInfo, SessionConfig]

::: alhazen.config.loader
    options:
      members: [load_model, load_rig, load_params, build_session_config]

::: alhazen.config.snapshot
    options:
      members: [build_provenance]

## Display and stimuli

::: alhazen.display.backend
    options:
      members: [DisplayBackend]

::: alhazen.display.screen
    options:
      members: [Screen]

::: alhazen.display.frames
    options:
      members: [FrameRecord, TrialFrameSummary, FrameMonitor, FrameTimeline]

::: alhazen.display.simulated
    options:
      members: [SimulatedDisplay]

::: alhazen.display.psychopy_backend
    options:
      members: [PsychoPyDisplay]

::: alhazen.display.text
    options:
      members: [reflow]

::: alhazen.stimuli.base
    options:
      members: [Stimulus, NullStimulus]

::: alhazen.stimuli.fixation
    options:
      members: [make_fixation]

## Scenes

::: alhazen.scenes.loader
    options:
      members: [load_scene, scene_param_names]

::: alhazen.scenes.model
    options:
      members: [Scene]

::: alhazen.scenes.render
    options:
      members: [SceneStimulus, headless_render]

## Devices

Each device class is a protocol and the values it hands over. The real
backends (`nidaq`, `eyelink`, `viewpixx`, `spikeglx`, …) are chosen by the rig
config and built by the session, so their classes are internal; the stand-ins
listed here are public because tests and the rehearsal modes construct them.

::: alhazen.devices.eyetracker.protocol
    options:
      members: [GazeSample, CalibrationTarget, CalibrationResult, CameraFrame,
        HostShape, EyeTracker]

::: alhazen.devices.eyetracker.messages
    options:
      members: [TrackerMessageSubscriber]

::: alhazen.devices.eyetracker.procedures
    options:
      members: [GazeCorrection]

::: alhazen.devices.reward
    options:
      members: [RewardDispenser, SimulatedReward]

::: alhazen.devices.sync
    options:
      members: [SyncOutput, SimulatedSync]

::: alhazen.devices.response
    options:
      members: [ResponseSample, ResponseDevice]

::: alhazen.devices.recording
    options:
      members: [RecordingSystem]

::: alhazen.devices.spikes
    options:
      members: [SpikeBatch, SpikeSource, SimulatedSpikeSource]

::: alhazen.devices.automated
    options:
      members: [AutomatedGazeTracker, AutomatedResponse]

## Neural arithmetic

::: alhazen.neural.rfmap
    options:
      members: [ProbeGrid, RFAccumulator]

## Data on disk

::: alhazen.data.manifest
    options:
      members: [write_manifest, add_to_manifest, verify_manifest]

## Analysis

::: alhazen.analysis.io.session
    options:
      members: [RunData, load_run, event_payloads]

::: alhazen.analysis.io.spikeglx
    options:
      members: [parse_meta, sample_rate_hz, channel_count, has_digital_word, n_samples,
        memmap_bin, digital_word_edges, analog_channel, find_run_files]

::: alhazen.analysis.io.kilosort
    options:
      members: [SpikeData, read_kilosort]

::: alhazen.analysis.io.eyelink
    options:
      members: [EyeLinkRecording, ensure_asc, read_asc]

::: alhazen.analysis.io.viewpixx
    options:
      members: [REAL_HEADER, DEFAULT_COLUMNS, GazeFrame, ClockFit, RecordingViews,
        GazeRecording, BinocularRecording, read_run, read_run_binocular, fit_clock,
        event_times]

::: alhazen.analysis.sync
    options:
      members: [AlignmentFit, event_bit_map, align_run, fit_alignment]

::: alhazen.analysis.results
    options:
      members: [ResultsBundle]

## The live monitor

::: alhazen.live_monitor.spec
    options:
      members: [LiveMonitorPanel, LiveMonitorSpec]

::: alhazen.live_monitor.panels
    options:
      members: [panel_payload]

### Deprecated spellings

Until 1.8 the live monitor was "the dashboard". These names resolve to the
classes above and warn when imported; they go in 2.0
([versioning](versioning.md) §4, and the table in [live monitor](live_monitor.md)).

::: alhazen.dashboard
    options:
      members: [DashboardPanel, DashboardSpec]

## Testing helpers

The public fakes an experiment package uses to test its own task.

::: alhazen.testing
    options:
      members: [FakeClock, FakeDisplay, FakeStimulus, ScriptedCommands, ScriptedInputs,
        EventCollector, ScriptedReward]

The sorted-spike publisher is a fake of a different kind: not a device a
session builds, but the external sorter a session *subscribes to*. See
[docs/pre-session-checkout.md](pre-session-checkout.md) for the rehearsal it
makes possible.

::: alhazen.testing.sorter
    options:
      members: [SorterSim, SortedSpikePublisher]

## Errors

::: alhazen.errors
    options:
      members: [AlhazenError, ConfigError, DisplayError, DataError, FrameQAError,
        SessionError, TrackerError, RewardError, RewardRequestError, SyncError,
        SpikeSourceError]
