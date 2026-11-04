"""Provider credit monitoring: verified balances or explicitly labeled local estimates."""
from datetime import UTC,datetime,timedelta
from decimal import Decimal,InvalidOperation
from uuid import uuid4
import re
import asyncio
import httpx
from pydantic import BaseModel,ConfigDict,EmailStr,Field,StrictBool,field_validator
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.db.models import OperationsSettings,ProviderAccount,ApiCostLog,OutboxJob
from app.db.session import session_scope
from app.core.settings import get_settings
from app.services import finance

PROVIDERS=('openai','xai','kie')
class BudgetValues(BaseModel):
    model_config=ConfigDict(extra='forbid')
    threshold:Decimal=Field(ge=0,le=1000000000,decimal_places=6)
    manual_balance:Decimal|None=Field(None,ge=0,le=1000000000,decimal_places=6)
class MonitorValues(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=0)
    staff_email:EmailStr
    alerts_enabled:StrictBool
    providers:dict[str,BudgetValues]
    @field_validator('providers')
    @classmethod
    def providers_known(cls,value):
        if set(value)!=set(PROVIDERS):raise ValueError('Configure OpenAI, xAI e KIE')
        return value

def quota_provider(error,default=None):
    name=type(error).__name__.lower();message=str(error).lower()
    if not (('credit' in message and 'insufficient' in message) or any(s in message for s in ['credit_balance_exhausted','insufficient_quota','no credits','not enough credits','insufficient balance','quota exceeded'])):return None
    if default:return default
    if 'xai' in name or 'xai http' in message:return 'xai'
    if 'kie' in name:return 'kie'
    return 'openai' if type(error).__module__.startswith('openai') else None

async def summary(session,row):
    value=None;source='unavailable';reason=None;as_of=row.checked_at
    if row.balance is not None:
        value=row.balance;source='provider'
    elif row.manual_balance is not None:
        value=row.manual_balance;as_of=row.manual_at;source='manual'
        if row.provider!='kie' and row.manual_at:
            config=await finance.settings(session)
            logs=(await session.execute(select(ApiCostLog).where(ApiCostLog.provider==row.provider,ApiCostLog.created_at>=row.manual_at).limit(50001))).scalars().all()
            amounts=[finance.cost_amount(log,config) for log in logs]
            if len(logs)>50000 or any(not known for _,known in amounts):
                value=None;reason='Há custos sem preço; não é possível estimar o saldo.'
            else:value=max(Decimal(0),value-Decimal(sum(v for v,_ in amounts))/1000000);source='local_estimate'
    if value is None and not reason:
        reason=('A chave OpenAI usada para geração não expõe saldo pré-pago. Informe o saldo para estimar somente o consumo registrado aqui.' if row.provider=='openai'
            else 'Configure XAI_MANAGEMENT_API_KEY e XAI_TEAM_ID no servidor ou informe o saldo para uma estimativa local.' if row.provider=='xai'
            else 'Não foi possível consultar o saldo KIE.')
    status='exhausted' if row.quota_exhausted else 'low' if value is not None and value<=row.threshold else 'ok' if value is not None else 'unknown'
    return {'provider':row.provider,'balance':value,'source':source,'unit':'credits' if row.provider=='kie' else 'USD','status':status,
            'threshold':row.threshold,'manual_balance':row.manual_balance,'manual_at':row.manual_at,'checked_at':row.checked_at,
            'as_of':as_of,'reason':reason,'error':row.error,'last_success_at':row.last_success_at,'last_alert_at':row.last_alert_at}

async def read(session):
    config=await session.get(OperationsSettings,1)
    rows=(await session.execute(select(ProviderAccount).order_by(ProviderAccount.provider))).scalars().all()
    return {'revision':config.revision,'staff_email':config.staff_email,'alerts_enabled':config.alerts_enabled,
            'providers':[await summary(session,row) for row in rows]}

async def save_settings(session,values):
    row=(await session.execute(select(OperationsSettings).where(OperationsSettings.id==1).with_for_update())).scalar_one()
    if row.revision!=values.revision:raise HTTPException(409,'Configurações alteradas; recarregue antes de salvar.')
    row.staff_email=str(values.staff_email);row.alerts_enabled=values.alerts_enabled;row.revision+=1
    for provider,value in values.providers.items():
        account=(await session.execute(select(ProviderAccount).where(ProviderAccount.provider==provider).with_for_update())).scalar_one()
        account.threshold=value.threshold
        if account.manual_balance!=value.manual_balance:
            account.manual_balance=value.manual_balance;account.manual_at=datetime.now(UTC) if value.manual_balance is not None else None
    await session.commit();return await read(session)

async def maybe_alert(session,row):
    result=await summary(session,row)
    low=result['status'] in {'low','exhausted'}
    if not low:return
    config=await session.get(OperationsSettings,1)
    if not config or not config.alerts_enabled:return
    now=datetime.now(UTC)
    if row.alert_episode and row.last_alert_at and row.last_alert_at>now-timedelta(hours=24):return
    row.alert_episode=str(uuid4());row.last_alert_at=now
    balance='indisponível' if result['balance'] is None else f"{result['balance']:.4f} {result['unit']}"
    provenance='informado pelo provedor' if result['source']=='provider' else 'estimativa local; não inclui outros aplicativos' if result['source']=='local_estimate' else 'informado manualmente' if result['source']=='manual' else 'saldo não disponível pela API'
    body=(f"Alerta de créditos — Recado Animado\n\nProvedor: {row.provider}\nEstado: {'créditos esgotados informados pela API' if row.quota_exhausted else 'saldo abaixo do limite configurado'}\n"
          f"Saldo: {balance} ({provenance})\nLimite de alerta: {row.threshold} {result['unit']}\n\n"
          'Verifique o saldo no provedor e recarregue se necessário. Os pedidos com falha podem ser retomados no painel após a recarga.\n'
          'https://hermes-persona-adminpanel.5tpiot.easypanel.host/manager/operations\n\nNenhum crédito de cliente foi alterado.')
    await session.execute(insert(OutboxJob).values(dedup_key=f'provider_alert:{row.provider}:{row.alert_episode}',kind='operational_email',
        payload={'to':config.staff_email,'subject':f'[Recado Animado] Créditos: {row.provider}','body':body}).on_conflict_do_nothing(index_elements=['dedup_key']))

async def report_error(error,provider=None):
    provider=quota_provider(error,provider)
    if provider not in PROVIDERS:return
    try:
        async with session_scope() as session:
            row=(await session.execute(select(ProviderAccount).where(ProviderAccount.provider==provider).with_for_update())).scalar_one_or_none()
            if not row:return
            row.quota_exhausted=True;row.error='credit_balance_exhausted';await maybe_alert(session,row)
    except Exception:
        from loguru import logger
        logger.warning('Could not record provider quota alert')

async def report_success(provider):
    try:
        async with session_scope() as session:
            row=(await session.execute(select(ProviderAccount).where(ProviderAccount.provider==provider).with_for_update())).scalar_one_or_none()
            if row:
                row.quota_exhausted=False;row.last_success_at=datetime.now(UTC)
                if row.error=='credit_balance_exhausted':row.error=None
    except Exception:pass

def decimal_balance(value):
    if isinstance(value,bool):raise ValueError('Invalid balance')
    number=Decimal(str(value))
    if not number.is_finite() or number<0:raise ValueError('Invalid balance')
    return number

async def poll_provider(provider):
    s=get_settings()
    async with httpx.AsyncClient(timeout=8) as client:
        if provider=='kie':
            if not s.kie_api_key:return None,'KIE_API_KEY não configurada.'
            response=await client.get(s.kie_api_base.rstrip('/')+'/chat/credit',headers={'Authorization':'Bearer '+s.kie_api_key})
            response.raise_for_status();data=response.json()
            if data.get('code')!=200:raise ValueError('Provider did not return a balance')
            return decimal_balance(data.get('data')),None
        if provider=='xai' and s.xai_management_api_key and s.xai_team_id:
            response=await client.get('https://management-api.x.ai/v1/billing/teams/'+s.xai_team_id+'/prepaid/balance',headers={'Authorization':'Bearer '+s.xai_management_api_key})
            response.raise_for_status()
            # The management API uses a signed USD-cent credit ledger (credits are negative).
            value=Decimal(str(response.json()['total']['val']))
            if not value.is_finite():raise ValueError('Invalid balance')
            return max(Decimal(0),-value/100),None
    return None,None

async def check_provider_balances(ctx=None):
    results = await asyncio.gather(*(poll_provider(p) for p in PROVIDERS), return_exceptions=True)
    for provider,result in zip(PROVIDERS,results):
        if isinstance(result,Exception):balance=None;error=f'Consulta indisponível ({type(result).__name__}).'
        else:balance,error=result
        async with session_scope() as session:
            row=(await session.execute(select(ProviderAccount).where(ProviderAccount.provider==provider).with_for_update())).scalar_one_or_none()
            if not row:continue
            row.balance=balance;row.checked_at=datetime.now(UTC)
            if error:row.error=error
            elif balance is not None:
                row.error=None;row.quota_exhausted=balance<=0
            await maybe_alert(session,row)
