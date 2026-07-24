"""Private and public postcard response contracts."""

from pydantic import BaseModel


class ArtworkResponse(BaseModel):
    image_url: str | None
    text_prompt: str


class PostcardResponse(BaseModel):
    id: str
    artwork: ArtworkResponse
    members: list[dict[str, object]]
    summary_text: str
    encouragement_text: str
    template_version: str
    created_at: str | None


class ShareResponse(BaseModel):
    share_token: str
    public_path: str


class PublicPostcardResponse(BaseModel):
    artwork: ArtworkResponse
    members: list[dict[str, object]]
    encouragement_text: str
    template_version: str
    noindex: bool = True
