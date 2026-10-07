/* The calibration-target choice in the Rig section: the pure part.
 *
 * What the rig says its calibration target is, what the page has chosen
 * instead, what to send to run.py, and the pulse the preview draws. No DOM:
 * tests/js/workspace_calibration.test.mjs loads this file on its own, the
 * browser loads it before workspace.js, which reads the global
 * `CalibrationChoice` and draws the controls.
 *
 * The Python side is the authority: alhazen.config.models
 * (CalibrationTargetConfig, with_calibration_target) validates what is sent,
 * and calibration_targets.pulse_scale draws the pulse on the rig. This file
 * mirrors only what the page needs to show the choice and its preview, and
 * the pulse formula is held to the Python one by a test of each.
 */
'use strict';

const CalibrationChoice = (() => {
  /* The trackers that draw a calibration target (models.py
   * TARGET_DRAWING_BACKENDS). */
  const DRAWING_BACKENDS = ['eyelink', 'viewpixx'];
  /* The modes that calibrate the rig's own tracker (modes.flag_refusal). */
  const CALIBRATING_MODES = ['run', 'test'];
  const APPEARANCES = [
    ['standard', 'Standard'],
    ['images', 'Chosen pictures'],
    ['random_images', 'Random pictures'],
  ];
  const MOTIONS = [['still', 'Still'], ['pulse', 'Pulsating']];
  const TAN_ONE_DEG = Math.tan(Math.PI / 180);

  /** Whether `rig` (its merged settings, /api/rig) has a tracker that draws
   *  a calibration target. */
  function drawsTarget(rig) {
    return DRAWING_BACKENDS.includes(rig?.devices?.eyetracker?.backend);
  }

  /** The rig's calibration target with the model's defaults filled in:
   *  {appearance, images, image_size_dva, motion, pulse}. `defaults` is what
   *  the project's alhazen reported at registration. */
  function fromRig(rig, defaults) {
    const own = rig?.devices?.eyetracker?.calibration_target || {};
    return {
      appearance: own.appearance ?? defaults.appearance,
      images: [...(own.images ?? defaults.images ?? [])],
      image_size_dva: own.image_size_dva ?? defaults.image_size_dva,
      motion: own.motion ?? defaults.motion,
      pulse: {...defaults.pulse, ...(own.pulse || {})},
    };
  }

  function sameList(a, b) {
    return a.length === b.length && a.every((value, index) => value === b[index]);
  }

  function sameSet(a, b) {
    return a.length === b.length && a.every((value) => b.includes(value));
  }

  /**
   * What a launch sends as `calibration_target`, or null to run the rig's
   * own. Only what differs is sent: run.py lays it over the rig's setting
   * (with_calibration_target). A changed appearance always sends its
   * pictures (none means every picture for random ones), since run.py starts
   * a changed appearance from no names; chosen pictures keep their order, a
   * random set does not have one.
   */
  function toSend(current, rig) {
    const out = {};
    if (current.appearance !== rig.appearance) {
      out.appearance = current.appearance;
      if (current.appearance !== 'standard' && current.images.length) {
        out.images = [...current.images];
      }
    } else if (current.appearance !== 'standard') {
      const same = current.appearance === 'images' ? sameList : sameSet;
      if (!same(current.images, rig.images)) out.images = [...current.images];
    }
    if (current.motion !== rig.motion) out.motion = current.motion;
    return Object.keys(out).length ? out : null;
  }

  /** Why the current choice cannot be launched, or null. */
  function problem(current) {
    if (current.appearance === 'images' && !current.images.length) {
      return 'Chosen pictures: pick at least one picture, or choose Random pictures.';
    }
    return null;
  }

  /** The pulse's size multiple at `elapsedS` after onset: the raised cosine
   *  alhazen draws on the rig (calibration_targets.pulse_scale). */
  function pulseScale(elapsedS, pulse) {
    const t = Math.max(elapsedS, 0);
    const swing = (1 - Math.cos(2 * Math.PI * pulse.rate_hz * t)) / 2;
    return pulse.min_scale + (pulse.max_scale - pulse.min_scale) * swing;
  }

  /** Screen px per degree on this monitor: the linear model alhazen's
   *  Screen uses (display/screen.py), or null when the rig does not say. */
  function pxPerDeg(monitor) {
    const {width_px: w, width_cm: cm, distance_cm: d} = monitor || {};
    if (!(w > 0 && cm > 0 && d > 0)) return null;
    return (w / cm) * d * TAN_ONE_DEG;
  }

  /** The target's still size in rig px (the standard one is 24 px across),
   *  or null when the monitor's geometry is unknown. */
  function stillPx(current, monitor) {
    if (current.appearance === 'standard') return 24;
    const scale = pxPerDeg(monitor);
    return scale === null ? null : current.image_size_dva * scale;
  }

  /** The choice in words, as alhazen's CalibrationTargetConfig.describe(). */
  function describe(current) {
    let what;
    if (current.appearance === 'standard') what = 'standard target';
    else if (current.appearance === 'images') what = `pictures ${current.images.join(', ')} in turn`;
    else what = `random pictures from ${current.images.length ? `${current.images.length} chosen` : 'all'}`;
    if (current.motion === 'still') return `${what}, still`;
    const p = current.pulse;
    return `${what}, pulsating ${p.min_scale}–${p.max_scale}× at ${p.rate_hz} Hz`;
  }

  return {
    APPEARANCES,
    CALIBRATING_MODES,
    MOTIONS,
    describe,
    drawsTarget,
    fromRig,
    problem,
    pulseScale,
    pxPerDeg,
    stillPx,
    toSend,
  };
})();

