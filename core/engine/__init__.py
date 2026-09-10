"""RoutePilot engine: algorithms and external-data interfaces.

* Stage 0 - provider **interfaces only** (:mod:`core.engine.providers`, D15/D16);
* Stage 1 - weighted scoring over implemented components (:mod:`core.engine.cost`) and
  first-stop candidate evaluation (:mod:`core.engine.first_stop`);
* Stage 2 - complete-route evaluation and the solver boundary
  (:mod:`core.engine.optimizer`).

No HTTP, UI, storage or vendor SDK is reachable from here (D1).
"""

__all__: list[str] = []
