"""The Alhazen Experiment Platform: the catalogue around alhazen experiments.

Browse experiments, read their methods bound to an exact source version, keep private
copies, change them and share versions back, on one of three hosts: a local single-user
host (``alhazen platform local``), a self-hosted multi-user server (``alhazen platform
serve``, the ``platform`` extra) and Mana (an integration built from the same web source).
The design is docs/experiment-platform/design.md.

Nothing here imports, executes or evaluates experiment code: experiment source is read as
text (git objects, Python AST, safe YAML). Nothing here controls devices or presents
stimuli; experiments run on a rig, in an alhazen session.

Importing this package is cheap and needs only alhazen's core dependencies; the server
host imports its optional web server lazily.
"""
