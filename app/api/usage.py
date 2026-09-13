"""Расход токенов в разрезе источника и канала (задача 12.7).

Месячный счётчик отвечает на вопрос «сколько потрачено», а решать
приходится другой: **какой канал жжёт бюджет.** Лимит сообщений и промпт
извлечения задаются каналу, значит и отключать надо канал — а по общему
числу выбирать нечего, кроме как наугад.

Наружу идут только публичные идентификаторы (контракт 9.10). Источник
может быть уже удалён — расход остаётся: история трат, исчезающая вместе
с тем, ради чего её вели, бесполезна.
"""

from fastapi import APIRouter, Depends

from app.db import TenantRepo
from app.deps import get_tenant_repo, require_user
from app.models import LLMUsageSlice, Monitor
from app.services.llm import MONTHLY_TOKEN_LIMIT, _period, _utcnow

router = APIRouter(dependencies=[Depends(require_user)])

DELETED_SOURCE_TITLE = "Удалённый источник"


@router.get("/api/usage")
async def usage(repo: TenantRepo = Depends(get_tenant_repo)) -> dict:
    """Расход текущего месяца: всего, по источникам, по каналам."""
    period = _period(_utcnow())
    rows = list(
        await repo.db.scalars(
            repo.query(LLMUsageSlice).where(LLMUsageSlice.period == period)
        )
    )
    titles = {
        m.public_id: m.title
        for m in await repo.db.scalars(repo.query(Monitor))
        if m.public_id
    }

    outside = 0
    sources: dict[str, dict] = {}
    for row in rows:
        if not row.source_public_id:
            # разбор вне источника: переразбор карточки из ленты
            outside += row.tokens
            continue
        source = sources.setdefault(
            row.source_public_id,
            {
                "public_id": row.source_public_id,
                "title": titles.get(row.source_public_id) or DELETED_SOURCE_TITLE,
                "tokens": 0,
                "summary_tokens": 0,
                "channels": [],
            },
        )
        source["tokens"] += row.tokens
        if row.chat_id == 0:
            # сведение по каналам — расход источника, а не канала
            source["summary_tokens"] += row.tokens
        else:
            source["channels"].append(
                {
                    "chat_id": row.chat_id,
                    "chat_title": row.chat_title or str(row.chat_id),
                    "tokens": row.tokens,
                }
            )

    # по убыванию: вопрос звучит «кто жжёт бюджет», а не «перечисли всех»
    for source in sources.values():
        source["channels"].sort(key=lambda c: c["tokens"], reverse=True)
    ordered = sorted(sources.values(), key=lambda s: s["tokens"], reverse=True)

    return {
        "period": period,
        "limit": MONTHLY_TOKEN_LIMIT,
        "total": sum(row.tokens for row in rows),
        "outside_sources": outside,
        "sources": ordered,
    }
