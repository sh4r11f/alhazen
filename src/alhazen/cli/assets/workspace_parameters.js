/* Schema helpers shared by the browser editor and its Node tests. */
'use strict';
const ParameterChoices = (() => {
  function resolve(value, root) {
    if (value?.$ref?.startsWith('#/')) {
      const target = value.$ref.slice(2).split('/').reduce((v, k) => v?.[k.replaceAll('~1', '/').replaceAll('~0', '~')], root);
      return {...target, ...value, $ref: undefined};
    }
    return value || {};
  }
  function field(root, path) {
    let current = root || {};
    for (const key of path) {
      current = resolve(current, root);
      if (!current.properties) {
        current = (current.anyOf || current.oneOf || []).map((v) => resolve(v, root)).find((v) => v.properties) || current;
      }
      current = current.properties?.[key] || {};
    }
    return resolve(current, root);
  }
  function enumValues(schema, root) {
    schema = resolve(schema, root);
    if (schema.enum) return schema.enum;
    if (Object.hasOwn(schema, 'const')) return [schema.const];
    return (schema.anyOf || schema.oneOf || []).flatMap((v) => enumValues(v, root));
  }
  function describe(root, path, value) {
    const schema = field(root, path);
    const multiple = Array.isArray(value) && value.every((v) => typeof v === 'string');
    if (!multiple && typeof value !== 'string') return null;
    const declared = enumValues(multiple ? schema.items || {} : schema, root).filter((v) => typeof v === 'string');
    const defaults = multiple ? (Array.isArray(schema.default) ? schema.default : []) : [schema.default];
    // Unconstrained strings have no finite valid set. Offer the model's default
    // and current value; YAML remains available for arbitrary custom strings.
    // Keyboard bindings additionally offer the common named keys and characters.
    const keys = !multiple && path.at(-1).endsWith('_key') && !declared.length
      ? ['space','return','escape','left','right','up','down','tab','backspace', ...'abcdefghijklmnopqrstuvwxyz0123456789'] : [];
    const choices = [...new Set([...declared, ...defaults, ...keys, ...(multiple ? value : [value])])].filter((v) => typeof v === 'string');
    return {multiple, choices};
  }
  function usesParameters(mode, scripts) {
    if (mode === 'measure') return false;
    const script = scripts.find((s) => s.id === mode);
    return script ? !!script.params_flag : true;
  }
  return {field, describe, usesParameters};
})();
