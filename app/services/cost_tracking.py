"""Attach future provider usage to its video without inventing prices for unknown models."""
from contextvars import ContextVar
from functools import wraps
from sqlalchemy import select
from app.db.models import ApiCostLog
from app.db.session import session_scope
from loguru import logger

item_context=ContextVar('generation_cost_item',default=None)


def tracked(function):
    @wraps(function)
    async def wrapped(*args,**kwargs):
        item_id=kwargs.get('item_id') or (getattr(args[0],'id',None) if args else None)
        token=item_context.set(item_id)
        try:return await function(*args,**kwargs)
        finally:item_context.reset(token)
    return wrapped


async def record_openai_usage(usage,model):
    if usage is None:return
    try:
        async with session_scope() as session:
            session.add(ApiCostLog(order_item_id=item_context.get(),provider='openai',operation='text_or_scene_review',
                model=model,cost_micro_usd=0,cost_source='usage_unpriced',usage={
                    'input_tokens':usage.prompt_tokens,'output_tokens':usage.completion_tokens}))
    except Exception:
        logger.warning('Could not record OpenAI usage; financial coverage is incomplete')


async def reconcile_xai_usage(request_id,result):
    ticks=(result.get('usage') or {}).get('cost_in_usd_ticks')
    if not isinstance(ticks,int) or isinstance(ticks,bool) or ticks<0:return
    try:
        async with session_scope() as session:
            row=(await session.execute(select(ApiCostLog).where(ApiCostLog.provider=='xai',ApiCostLog.provider_request_id==request_id))).scalar_one_or_none()
            if row:
                from app.services.finance import money
                from decimal import Decimal
                row.cost_micro_usd=money(Decimal(ticks)/10000)
                row.cost_source='provider';row.usage={'cost_in_usd_ticks':ticks}
    except Exception:logger.warning('Could not reconcile xAI usage; recorded estimate retained')
