"""AI-assisted experiment authoring (docs/hub/ai.md).

Users bring their own provider keys (stored encrypted, `keys`); a draft's
plan and source are produced by jobs (`jobs`) that call the user's provider
(`providers`) through the authoring kit (`author`, `prompts`, `schemas`);
`drafts` is the HTTP-facing service and `routes` wires it into the app.
Nothing here imports or executes generated code, installs anything,
touches a rig or publishes: a person accepts a validated draft, which
creates a private version through the ordinary upload pipeline.
"""
