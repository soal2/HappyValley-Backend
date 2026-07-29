"""Import all models so Alembic and Base.metadata can discover them."""

from app.models.character import Character  # noqa: F401
from app.models.feedback import Feedback, FeedbackLike  # noqa: F401
from app.models.gratitude_card import GratitudeCard  # noqa: F401
from app.models.meditation_card import MeditationCard  # noqa: F401
from app.models.postcard import GeneratedImage, Postcard  # noqa: F401
from app.models.queue import QueueEntry  # noqa: F401
from app.models.room import ActivityRoom, RoomMessage  # noqa: F401
from app.models.room_character import RoomCharacter  # noqa: F401
from app.models.user import User, UserProfile  # noqa: F401
