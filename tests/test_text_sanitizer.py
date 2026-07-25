"""用户文本净化：静默清除脏话/侮辱词，避免上游内容审核拒绝（如 451）。"""

from app.services.text_sanitizer import _GENTLE_PLACEHOLDER, sanitize_user_text


def test_removes_english_profanity_but_keeps_the_rest() -> None:
    # 真实触发 451 的贡献文本：脏话被抹掉，其余原样保留。
    assert sanitize_user_text("fuck the duck") == "the duck"
    assert sanitize_user_text("FUCK this") == "this"
    assert sanitize_user_text("shit happens") == "happens"


def test_catches_spaced_and_masked_variants() -> None:
    assert sanitize_user_text("f u c k you") == "you"
    assert sanitize_user_text("fuuuck") == _GENTLE_PLACEHOLDER


def test_does_not_touch_innocent_lookalikes() -> None:
    # 词边界正确：不能误伤正常词。
    assert sanitize_user_text("the ducking pond") == "the ducking pond"
    assert sanitize_user_text("阳光、花草和柔和的天空") == "阳光、花草和柔和的天空"


def test_removes_chinese_profanity() -> None:
    assert sanitize_user_text("你是傻逼吧") == "你是吧"
    assert sanitize_user_text("傻逼") == _GENTLE_PLACEHOLDER


def test_all_profanity_falls_back_to_gentle_placeholder() -> None:
    assert sanitize_user_text("fuck 傻逼 shit") == _GENTLE_PLACEHOLDER
