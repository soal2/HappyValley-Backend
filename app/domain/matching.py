"""Matching engine: rule-based team selection for painting therapy activity."""

from app.models.character import Character


class MatchingEngine:
    """Deterministic rule-based matching for AI character team selection."""

    def __init__(self, characters: list[Character]):
        self.characters = characters

    def match_team(self, user_age: int, exclude_ids: list[str] | None = None) -> list[Character]:
        """
        Select 4 characters satisfying:
        - 2 "been-there" + 1 "different-perspective" + 1 "quiet-resonator"
        - Prefer age within ±5 years of user
        - No duplicates
        - Deterministic given same inputs (for fallback)
        """
        exclude_ids = exclude_ids or []

        # Filter out excluded characters
        available = [c for c in self.characters if c.id not in exclude_ids]

        # Categorize by personality type
        been_there = [c for c in available if c.personality_type == "been-there"]
        different_perspective = [
            c for c in available if c.personality_type == "different-perspective"
        ]
        quiet_resonator = [c for c in available if c.personality_type == "quiet-resonator"]

        # Sort each category by age proximity to user
        been_there_sorted = self._sort_by_age_proximity(been_there, user_age)
        diff_perspective_sorted = self._sort_by_age_proximity(different_perspective, user_age)
        quiet_resonator_sorted = self._sort_by_age_proximity(quiet_resonator, user_age)

        # Pick: 2 from been-there, 1 from different-perspective, 1 from quiet-resonator
        team: list[Character] = []
        team.extend(been_there_sorted[:2])
        team.extend(diff_perspective_sorted[:1])
        team.extend(quiet_resonator_sorted[:1])

        return team

    def _sort_by_age_proximity(self, characters: list[Character], user_age: int) -> list[Character]:
        """Sort characters by how close their age range midpoint is to user_age."""

        def age_distance(c: Character) -> float:
            midpoint = (c.age_range_start + c.age_range_end) / 2
            return abs(midpoint - user_age)

        return sorted(characters, key=age_distance)
