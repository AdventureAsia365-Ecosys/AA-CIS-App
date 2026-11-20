"""CMS Adapter interface — PRD v1.0 §4.1"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BlogContent:
    title: str
    content_html: str
    slug: str
    seo_title: str
    seo_meta: str
    status: str = "draft"  # Always 'draft' per PRD v1.0 Q10 — human publishes manually
    # AA-1168 — extra REST post meta (the AI mark, services/acp_content_writing/ai_marking.wordpress_post_meta);
    # WordPress stores only the keys the site registered and ignores the rest.
    meta: dict = field(default_factory=dict)


@dataclass
class CMSPostResult:
    post_id: int
    post_url: str
    status: str
    cms_type: str


class CMSAdapter(ABC):
    @abstractmethod
    async def create_post(self, content: BlogContent) -> CMSPostResult: ...
