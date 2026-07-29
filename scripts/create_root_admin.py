"""Create / sync the root super-admin user.

Idempotent: safe to re-run.
- If root user missing: create root + profile + copy all postcards.
- If root user exists: leave user/profile/postcards untouched.
- Always: reset root's meditation_cards to the full 14-image catalog
  (5 scenes × 2-3 cards, quotes from frontend config.ts).

Usage: python -m scripts.create_root_admin
"""

import asyncio

from sqlalchemy import delete, select

from app.database import async_session_factory
from app.models.meditation_card import MeditationCard
from app.models.postcard import Postcard
from app.models.room import ActivityRoom
from app.models.user import User, UserProfile
from app.services.auth_service import _hash_password

USERNAME = "root"
PASSWORD = "root1234"
MBTI = "ENTJ"
APPEARANCE_ID = "xqg-entj-commander"  # 前端 src/lib/mbti-profile.ts 中 ENTJ 对应的形象 ID

# 14 张磁盘冥想卡的 (scene_key, card_file, quote) 全量映射
# quotes 取自 HappyValley-Frontend/src/lib/meditation/config.ts 中各场景 quotes 数组按 card 索引
MEDITATION_CARDS: list[tuple[str, list[tuple[str, str]]]] = [
    ("beach", [
        ("card_01.jpg", "听潮起潮落，心也跟着柔软下来。"),
        ("card_02.jpg", "篝火映着星河，你被整个宇宙温柔包围。"),
        ("card_03.jpg", "把烦恼写进沙里，让浪花带回海里。"),
    ]),
    ("meadow", [
        ("card_01.jpg", "把夕阳收进心里，留一整年的暖。"),
        ("card_02.jpg", "花海轻摇，像在替你深呼吸。"),
        ("card_03.jpg", "余晖洒落之处，皆是温柔以待。"),
    ]),
    ("waterfall", [
        ("card_01.jpg", "听水声奔涌，心却愈发清澈。"),
        ("card_02.jpg", "让雾气洗净思绪，让绿意填满呼吸。"),
        ("card_03.jpg", "每一道彩虹，都是雨后的温柔承诺。"),
    ]),
    ("aurora-winter-town", [
        ("card_01.jpg", "雪落无声，心亦安然。"),
        ("card_02.jpg", "让极光点亮心底那片宁静的夜。"),
        ("card_03.jpg", "万物沉睡时，你在自己的光里醒来。"),
    ]),
    ("painting-house", [
        ("card_01.jpg", "翻开一页，也翻开内心的一处安静。"),
        ("card_02.jpg", "藤蔓静静生长，像你慢慢成为自己。"),
    ]),
]


async def sync_root() -> None:
    async with async_session_factory() as session:
        existing = (
            await session.execute(select(User).where(User.username == USERNAME))
        ).scalar_one_or_none()

        if existing is None:
            root = User(username=USERNAME, password_hash=_hash_password(PASSWORD))
            session.add(root)
            await session.flush()
            session.add(UserProfile(
                user_id=root.id,
                mbti=MBTI,
                appearance_id=APPEARANCE_ID,
                completed=True,
            ))

            pc_rows = (await session.execute(select(Postcard))).scalars().all()
            for p in pc_rows:
                new_room = ActivityRoom(
                    user_id=root.id,
                    status="completed",
                    current_stage="completed",
                )
                session.add(new_room)
                await session.flush()
                session.add(
                    Postcard(
                        user_id=root.id,
                        room_id=new_room.id,
                        image_path=p.image_path,
                        encouragement_text=p.encouragement_text,
                        summary_text=p.summary_text,
                        artwork_text=p.artwork_text,
                        generation_meta=p.generation_meta,
                        member_data=p.member_data,
                        template_version=p.template_version,
                        share_token=None,
                        shared_at=None,
                    )
                )
            postcards_added = len(pc_rows)
            mode = "created"
        else:
            root = existing
            postcards_added = 0
            mode = "exists"
            # 已存在 root：补齐 appearance_id（首次建用户时漏设）
            profile = (
                await session.execute(select(UserProfile).where(UserProfile.user_id == root.id))
            ).scalar_one_or_none()
            if profile is not None and profile.appearance_id != APPEARANCE_ID:
                profile.appearance_id = APPEARANCE_ID

        # meditation_cards: 始终 DELETE + INSERT 14 条
        await session.execute(
            delete(MeditationCard).where(MeditationCard.user_id == root.id)
        )
        cards_added = 0
        for scene_key, items in MEDITATION_CARDS:
            for card_file, quote in items:
                session.add(
                    MeditationCard(
                        user_id=root.id,
                        scene_key=scene_key,
                        card_file=card_file,
                        quote=quote,
                        nickname=USERNAME,
                        mbti=MBTI,
                    )
                )
                cards_added += 1

        await session.commit()

    print(
        f"✓ root ({mode}): id={root.id}, "
        f"postcards={'+' if postcards_added else ''}{postcards_added}, "
        f"meditation_cards重置为{cards_added} 条"
    )


def main() -> None:
    print("=== 同步超级管理员 root ===\n")
    asyncio.run(sync_root())


if __name__ == "__main__":
    main()
