/* Alhazen experiment workspace — schema helpers for the parameter editor.
 *
 * ParameterChoices answers one question for workspace.js: given the task's
 * JSON schema (from /api/schema), the dotted path of a parameter and its
 * current value, which finite set of strings may it take? A field with such
 * a set becomes a dropdown (a multi-select for a list of strings); anything
 * else keeps a free-text control, and the text editor remains the way to
 * type a string the schema does not list.
 *
 * ParameterSections answers a second one: under which heading of the editor
 * (Stimulus, Timing, Session, Other) does a parameter's row go? See below.
 *
 * Kept apart from workspace.js, with no DOM access, so the Node tests in
 * tests/js/workspace_parameters.test.mjs can load it on its own; the browser
 * loads it first and workspace.js reads the globals `ParameterChoices` and
 * `ParameterSections`.
 */
'use strict';

const ParameterChoices = (() => {
  /**
   * Follow a local `$ref` ("#/$defs/Name") to its target and lay the
   * referring object's own keys over it, so a `default` written beside the
   * $ref wins. Anything without a $ref is returned as is, and a missing
   * value becomes {} so callers can read `.properties` without guarding.
   */
  function resolve(value, root) {
    if (value?.$ref?.startsWith('#/')) {
      // The fragment is a JSON pointer: one step per '/', with the pointer's
      // escapes undone in each step ("~1" is "/", then "~0" is "~" — in that
      // order, or "~01" would decode to "/" instead of "~1").
      const steps = value.$ref.slice(2).split('/');
      const target = steps.reduce(
        (current, key) => current?.[key.replaceAll('~1', '/').replaceAll('~0', '~')],
        root,
      );
      return {...target, ...value, $ref: undefined};
    }
    return value || {};
  }

  /**
   * The sub-schema for a parameter at `path` (an array of keys). Pydantic
   * writes an optional nested model as anyOf [model, null]; when the current
   * level has no `properties` of its own, the first alternative that does is
   * followed, so fields inside optional groups still get their choices.
   */
  function field(root, path) {
    let current = root || {};
    for (const key of path) {
      current = resolve(current, root);
      if (!current.properties) {
        const alternatives = (current.anyOf || current.oneOf || []).map((v) => resolve(v, root));
        current = alternatives.find((v) => v.properties) || current;
      }
      current = current.properties?.[key] || {};
    }
    return resolve(current, root);
  }

  /**
   * Every literal a schema allows: an `enum`, a `const`, or the union of
   * its anyOf/oneOf alternatives (a Literal["a"] | Literal["b"] field
   * arrives as alternatives, each with a const).
   */
  function enumValues(schema, root) {
    schema = resolve(schema, root);
    if (schema.enum) return schema.enum;
    if (Object.hasOwn(schema, 'const')) return [schema.const];
    return (schema.anyOf || schema.oneOf || []).flatMap((v) => enumValues(v, root));
  }

  /**
   * The dropdown for a parameter, or null when it should keep a free-text
   * control. Returns {multiple, choices}: `multiple` for a list of strings,
   * `choices` the strings to offer, deduplicated, in this order: what the
   * schema declares, the model's default(s), then the current value(s).
   */
  function describe(root, path, value) {
    const schema = field(root, path);
    // Only strings and lists of strings are this helper's business; numbers,
    // booleans and mixed arrays keep the editors renderEditor gives them.
    const multiple = Array.isArray(value) && value.every((v) => typeof v === 'string');
    if (!multiple && typeof value !== 'string') return null;
    // What the schema allows — for a list, what its items allow.
    const declared = enumValues(multiple ? schema.items || {} : schema, root)
      .filter((v) => typeof v === 'string');
    // The model's default is always offered, so a reader can go back to it.
    let defaults;
    if (multiple) defaults = Array.isArray(schema.default) ? schema.default : [];
    else defaults = [schema.default];
    // Unconstrained strings have no finite valid set. Offer the model's default
    // and current value; YAML remains available for arbitrary custom strings.
    // Keyboard bindings additionally offer the common named keys and characters.
    const isKeyBinding = !multiple && path.at(-1).endsWith('_key') && !declared.length;
    const keys = isKeyBinding
      ? [
        'space', 'return', 'escape', 'left', 'right', 'up', 'down', 'tab', 'backspace',
        ...'abcdefghijklmnopqrstuvwxyz0123456789',
      ]
      : [];
    const current = multiple ? value : [value];
    // A non-string default (null for an optional field) is dropped by the
    // final filter rather than shown as a choice.
    const choices = [...new Set([...declared, ...defaults, ...keys, ...current])]
      .filter((v) => typeof v === 'string');
    return {multiple, choices};
  }

  /**
   * Whether a launch in `mode` takes task parameters at all. Measuring the
   * rig never does; a discovered script does only if it declared a
   * parameter-file flag; the built-in session and movie modes always do.
   */
  function usesParameters(mode, scripts) {
    // Measure rig takes none, and a Training stage names its own.
    if (mode === 'measure' || mode === 'training') return false;
    const script = scripts.find((s) => s.id === mode);
    return script ? !!script.params_flag : true;
  }

  return {field, describe, usesParameters, resolve};
})();

/* ------------------------------------------------------------------ */

const ParameterSections = (() => {
  /* The sections, in the order the editor shows them; Other, last, is also
   * where a row goes that nothing declares and no rule below places.
   * alhazen.config.sections.SECTIONS holds the same list
   * (tests/unit/test_param_sections.py checks). */
  const SECTIONS = ['stimulus', 'timing', 'session', 'other'];
  const KEY = 'x-alhazen-section';
  const ORDER = SECTIONS;
  const LABELS = {stimulus: 'Stimulus', timing: 'Timing', session: 'Session', other: 'Other'};
  const {resolve} = ParameterChoices;

  /*
   * Inference, for a field nothing declares (an experiment on an alhazen
   * older than sections, or a field its author left undeclared). Drawn from
   * the four experiments' real params models (2026-10-14). A row is judged
   * by its top-level field name — so a group (paradigm, reward, block,
   * simulation, ...) stays together under one heading — split into words at
   * the underscores. The first rule that matches wins:
   *
   *   1. a Duration ({ms} or {frames}), or a name ending in _ms, _frames, or
   *      _s that is not a rate (_dva_s, _rev_s, _deg_s ...)      -> timing
   *   2. a session word (paradigm, block, trials, repeats, order, reward,
   *      subject, ...) or n_per_...                               -> session
   *   3. a response criterion (window, tolerance, threshold, clearance, key,
   *      error, settle): spatial or not, it is not the stimulus    -> other
   *   4. a timing word (timeout, duration, jitter, iti, hold, delay, ...)
   *                                                               -> timing
   *   5. a spatial unit last (_dva, _deg, _cpd, _px, _cm), a rate (_dva_s,
   *      _rev_s, _deg_s) or a stimulus word (color, contrast, size, speed,
   *      coherence, dots, stimuli, gabor, ...)                    -> stimulus
   *   6. anything else                                            -> other
   */
  const RATE_UNITS = new Set(['dva', 'deg', 'rev', 'px', 'cm', 'cyc', 'cycles']);
  const SPATIAL_UNITS = new Set(['dva', 'deg', 'cpd', 'px', 'cm']);
  const SESSION_WORDS = new Set([
    'paradigm', 'block', 'blocks', 'trial', 'trials', 'repeat', 'repeats', 'break', 'breaks',
    'session', 'failures', 'order', 'shuffle', 'seed', 'reward', 'subject', 'stages',
  ]);
  const RESPONSE_WORDS = new Set([
    'window', 'tolerance', 'threshold', 'clearance', 'key', 'keys', 'error', 'settle',
  ]);
  const TIMING_WORDS = new Set([
    'timeout', 'duration', 'jitter', 'iti', 'hold', 'dwell', 'delay', 'interval', 'soa',
    'latency', 'lifetime', 'period', 'gap',
  ]);
  const STIMULUS_WORDS = new Set([
    'color', 'colour', 'colors', 'colours', 'contrast', 'contrasts', 'luminance', 'palette',
    'palettes', 'background', 'ink', 'size', 'radius', 'diam', 'diameter', 'eccentricity', 'ecc',
    'speed', 'speeds', 'coherence', 'coherences', 'dot', 'dots', 'stimulus', 'stimuli', 'gabor',
    'orientation', 'pedestal', 'aperture', 'opacity',
  ]);
  const DURATION_KEYS = new Set(['ms', 'frames']);

  /** The section a field infers to, and the rule that said so. */
  function infer(path) {
    const name = String(path[0] ?? '');
    const words = name.toLowerCase().split('_').filter(Boolean);
    const last = words.at(-1);
    const beforeLast = words.at(-2);
    const rate = last === 's' && RATE_UNITS.has(beforeLast);
    const has = (set) => words.some((w) => set.has(w));
    if (path.length === 2 && DURATION_KEYS.has(path[1])) return {section: 'timing', rule: 'duration'};
    if (last === 'ms' || last === 'frames' || (last === 's' && words.length > 1 && !rate)) {
      return {section: 'timing', rule: 'time unit'};
    }
    if (has(SESSION_WORDS) || name.startsWith('n_per_')) return {section: 'session', rule: 'session word'};
    if (has(RESPONSE_WORDS)) return {section: 'other', rule: 'response criterion'};
    if (has(TIMING_WORDS)) return {section: 'timing', rule: 'timing word'};
    if (rate || SPATIAL_UNITS.has(last)) return {section: 'stimulus', rule: 'spatial unit'};
    if (has(STIMULUS_WORDS)) return {section: 'stimulus', rule: 'stimulus word'};
    return {section: 'other', rule: 'no rule'};
  }

  /* A schema node's own declaration: its KEY, or one of its alternatives'
   * (an optional field is anyOf [model, null], and the model's own config
   * sits on the model). Only a known name counts: anything else is read as
   * no declaration, so a newer alhazen's section never breaks this page. */
  function declared(node, root) {
    const own = resolve(node, root);
    if (SECTIONS.includes(own[KEY])) return own[KEY];
    for (const alt of [...(own.anyOf || []), ...(own.oneOf || []), ...(own.allOf || [])]) {
      const found = declared(alt, root);
      if (found) return found;
    }
    return null;
  }

  /* The schema node of each step of `path`, outermost first. A key the
   * object does not name falls to its additionalProperties (a dict of
   * models: palettes.arcs is a PaletteParams). */
  function steps(root, path) {
    const nodes = [];
    let current = root || {};
    for (const key of path) {
      current = resolve(current, root);
      if (!current.properties && !current.additionalProperties) {
        const alts = (current.anyOf || current.oneOf || []).map((v) => resolve(v, root));
        current = alts.find((v) => v.properties || v.additionalProperties) || current;
      }
      const props = current.properties || {};
      if (Object.hasOwn(props, key)) current = props[key];
      else if (current.additionalProperties && typeof current.additionalProperties === 'object') {
        current = current.additionalProperties;
      } else current = {};
      nodes.push(current);
    }
    return nodes;
  }

  /**
   * Where the row for `path` goes: {section, how, rule?}. `how` is
   * 'declared' (the field says), 'inherited' (an enclosing field or model
   * says), or 'inferred' (infer() above; Other when no rule matches).
   */
  function of(root, path) {
    const nodes = steps(root, path);
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
      const found = declared(nodes[i], root);
      if (found) return {section: found, how: i === nodes.length - 1 ? 'declared' : 'inherited'};
    }
    return {...infer(path), how: 'inferred'};
  }

  return {SECTIONS, ORDER, LABELS, KEY, of, infer};
})();
