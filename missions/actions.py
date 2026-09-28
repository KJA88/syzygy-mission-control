"""Future RoArm mission steps must go through the Phase 3 skill owner.

The reference switch mission does not import or call this adapter.
Mission STOP is not RoArm torque-off and does not call the skill owner.
"""
from __future__ import annotations

SKILLS = frozenset({"move_to_pose", "return_home", "run_pattern", "stop", "clear"})


class SkillActionPort:
    def __init__(self, owner):
        self.owner = owner

    def request(self, skill, params=None, mission_id=None, trace_id=None):
        if skill not in SKILLS:
            return {"accepted": False, "result": "rejected", "reason": "UNKNOWN_SKILL"}
        if self.owner is None:
            return {"accepted": False, "result": "rejected", "reason": "CONTROL_OWNER_UNAVAILABLE"}
        return self.owner.request(
            skill,
            authority="mission-engine",
            operator="mission-engine",
            mission=mission_id,
            trace_id=trace_id,
            params=params if isinstance(params, dict) else {},
        )
