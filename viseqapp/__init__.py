"""viseqapp — the viseq application package (REFACTOR_LATEST.md).

Refactor target: split the single-file ``viseq.py`` into this package,
keeping ``viseq.py`` as the composition root (entry point + boot + main
loop). Modules are added bottom-up in dependency order; the public API is
re-exported here and by the ``viseq`` facade so the test harness keeps
working unchanged.

Conventions (see specs/REFACTOR_LATEST.md):
- ``state.py`` owns every mutable global; access as ``state.NAME``.
- Worker modules (osc/audio/sequencer/midi) never import ``dpg``.
- No renames: public names stay byte-identical.
"""
