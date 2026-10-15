"""Native AI-assisted experiment authoring with the user's own provider key
(docs/hub/ai.md).

- ``keys``: users' provider keys, encrypted at rest under the operator's
  wrapping key; never returned, logged or sent to the browser.
- ``providers``: one small HTTP client per provider behind one interface.
- ``author``, ``prompts``, ``schemas``: the authoring kit, which turns a
  description into a reviewed plan and a statically validated private
  package; ``prompts`` and ``schemas`` hold what is sent to a provider and
  the shapes it must answer in.
- ``jobs``: the worker that runs plan and source jobs (lease, fencing,
  cancellation, usage and disclosure records).
- ``drafts``: the HTTP-facing service (draft lifecycle, admission,
  acceptance through the ordinary upload pipeline); ``routes`` wires it in.

Nothing here imports or runs generated code, installs anything, touches a
rig or publishes: a person accepts a validated draft, which creates a
private version.
"""
