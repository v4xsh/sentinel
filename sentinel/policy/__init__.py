"""Policy engine — pure-function decision layer.

Only ``decide(state) -> list[Action]`` and the ``check_invariants(answer)``
helper are public. Rules R1–R10 + §3a triggers are encoded as pure Python
against the shape defined in ``docs/POLICY_EXTRACT.md``.
"""

from sentinel.policy.policy_engine import (  # noqa: F401
    ACTIONS_AUTO, ACTIONS_L1_TABLE, ACTIONS_L2_TABLE,
    Action, PatternEnum, PolicyDecision, PolicyInput, VerdictEnum,
    decide, route_for,
)
from sentinel.policy.invariants import check_invariants  # noqa: F401
