"""Matching engine: rule-based team selection for painting therapy activity."""

import random

from app.models.character import Character


class MatchingEngine:
    """Rule-based matching for AI character team selection.

    放宽年龄约束 + 引入随机性：不再严格取年龄最接近的前几名，
    而是从年龄排序的「短名单」里随机抽样，保证每次活动遇到不同团友，
    同时仍优先年龄阶段相近的人物。
    """

    def __init__(self, characters: list[Character], rng: random.Random | None = None):
        self.characters = characters
        self._rng = rng or random.Random()

    def match_team(self, user_age: int, exclude_ids: list[str] | None = None) -> list[Character]:
        """
        Select 4 characters satisfying:
        - 2 "been-there" + 1 "different-perspective" + 1 "quiet-resonator"
        - Prefer age within proximity of user (via sorted shortlist)
        - No duplicates
        - 每组从年龄排序的短名单里随机选取，避免每次匹配到同一批人
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

        # Pick: 2 from been-there, 1 from different-perspective, 1 from quiet-resonator.
        # 每组从年龄排序的前 N 名短名单里随机抽取，放宽年龄限制并增加多样性。
        team: list[Character] = []
        team.extend(self._pick_from_shortlist(been_there_sorted, need=2, shortlist_size=6))
        team.extend(self._pick_from_shortlist(diff_perspective_sorted, need=1, shortlist_size=4))
        team.extend(self._pick_from_shortlist(quiet_resonator_sorted, need=1, shortlist_size=4))

        return team

    def _pick_from_shortlist(
        self, sorted_pool: list[Character], need: int, shortlist_size: int
    ) -> list[Character]:
        """从年龄排序的候选池里取前 shortlist_size 名组成短名单，再随机选 need 个。

        - 短名单保证年龄不会偏离用户太远（仍优先接近的）；
        - 随机抽样保证多次匹配结果不同，避免总遇到同一批人；
        - 候选不足时退化为全取。
        """
        shortlist = sorted_pool[:shortlist_size]
        if len(shortlist) <= need:
            return shortlist
        return self._rng.sample(shortlist, need)

    def _sort_by_age_proximity(self, characters: list[Character], user_age: int) -> list[Character]:
        """Sort characters by how close their age range midpoint is to user_age."""

        def age_distance(c: Character) -> float:
            midpoint = (c.age_range_start + c.age_range_end) / 2
            return abs(midpoint - user_age)

        return sorted(characters, key=age_distance)
