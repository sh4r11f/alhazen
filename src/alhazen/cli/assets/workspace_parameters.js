/* Alhazen experiment workspace — schema helpers for the parameter editor.
 *
 * ParameterChoices answers one question for workspace.js: given the task's
 * JSON schema (from /api/schema), the dotted path of a parameter and its
 * current value, which finite set of strings may it take? A field with such
 * a set becomes a dropdown (a multi-select for a list of strings); anything
 * else keeps a free-text control, and the text editor remains the way to
 * type a string the schema does not list.
 *
 * Kept apart from workspace.js, with no DOM access, so the Node tests in
 * tests/js/workspace_parameters.test.mjs can load it on its own; the browser
 * loads it first and workspace.js reads the global `ParameterChoices`.
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
    if (mode === 'measure') return false;
    const script = scripts.find((s) => s.id === mode);
    return script ? !!script.params_flag : true;
  }

  return {field, describe, usesParameters};
})();
