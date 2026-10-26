from datetime import UTC,datetime
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.v1 import admin_finance
from app.db.session import get_session
from app.services import finance


def fixtures():
    items=[NS(id=1,sequence=1,status='READY',character_ids=['bela']),NS(id=2,sequence=2,status='READY',character_ids=['bluey'])]
    order=NS(id=1,status='DELIVERED',items=items,created_at=datetime.now(UTC),paid_at=datetime.now(UTC),recipient_name='Buyer',guest_email='buyer@example.com',total_cents=10000)
    payment=NS(id=1,provider='mercadopago',provider_id='123',funds_order=True,status='APPROVED',amount_cents=10000,raw_webhook={'fee_details':[{'amount':'3.50'}]})
    logs=[NS(order_item_id=i,operation='video_generation',cost_micro_usd=i*1_000_000,cost_source='estimated') for i in [1,2]]
    return order,payment,logs


def test_brl_conversion_and_all_order_costs_reconcile_to_cents():
    order,payment,logs=fixtures()
    config=finance.FinanceValues(usd_brl=Decimal('5'),overhead_per_video_cents=20)
    note=NS(is_test=False,note=None,fee_override_cents=None,additional_cost_cents=40)
    result=finance.order_contribution(order,[payment],logs,config,note,commission_pct=Decimal('10'))
    assert result['cost_brl_cents']==1500
    assert result['fee_cents']==350
    assert result['contribution_cents']==7070
    assert sum(i['contribution_cents'] for i in result['items'])==7070
    assert sum(i['revenue_cents'] for i in result['items'])==10000


def test_regeneration_attempts_reduce_the_original_videos_margin():
    order,payment,logs=fixtures();config=finance.FinanceValues(usd_brl=Decimal('5'))
    before=finance.order_contribution(order,[payment],logs,config)
    logs.append(NS(order_item_id=1,operation='video_generation',cost_micro_usd=1_000_000,cost_source='estimated'))
    after=finance.order_contribution(order,[payment],logs,config)
    assert before['contribution_cents']-after['contribution_cents']==500
    assert after['items'][0]['video_attempts']==2


def test_missing_fees_or_exchange_rate_do_not_become_zero_profit_costs():
    order,payment,logs=fixtures();payment.raw_webhook={}
    result=finance.order_contribution(order,[payment],logs,finance.FinanceValues())
    assert result['contribution_cents'] is None and result['fee_cents'] is None
    assert 'Câmbio USD/BRL não configurado' in result['missing']
    assert 'Taxa de pagamento não conciliada' in result['missing']


def test_explicit_zero_provider_fee_is_known_and_overrides_estimate():
    order,payment,logs=fixtures();payment.raw_webhook={'fee_details':[]}
    result=finance.order_contribution(order,[payment],logs,finance.FinanceValues(usd_brl=Decimal('5'),estimated_fee_bps=500))
    assert result['fee_cents']==0 and result['fee_source']=='provider'
    assert result['contribution_cents']==8500


def test_refund_retains_generation_cost_and_retained_fee():
    order,payment,logs=fixtures();order.status='REFUNDED';payment.status='REFUNDED'
    result=finance.order_contribution(order,[payment],logs,finance.FinanceValues(usd_brl=Decimal('5')))
    assert result['net_revenue_cents']==0 and result['refunded_cents']==10000
    assert result['contribution_cents']==-1850


def test_credit_redemption_does_not_invent_cash_revenue():
    order,payment,logs=fixtures();payment.provider='credits';payment.amount_cents=0;order.total_cents=0;payment.raw_webhook=None
    result=finance.order_contribution(order,[payment],logs,finance.FinanceValues(usd_brl=Decimal('5')))
    assert result['gross_cents']==0 and result['fee_cents']==0 and result['contribution_cents']==-1500


def test_unpriced_openai_usage_is_not_treated_as_free():
    order,payment,logs=fixtures()
    usage=NS(order_item_id=1,operation='text_or_scene_review',cost_micro_usd=0,cost_source='usage_unpriced',model='test-model',usage={'input_tokens':1000,'output_tokens':100})
    logs.append(usage);config=finance.FinanceValues(usd_brl=Decimal('5'))
    assert finance.order_contribution(order,[payment],logs,config)['contribution_cents'] is None
    config.model_rates={'test-model':finance.ModelRate(input_micro_usd_per_million=1_000_000,output_micro_usd_per_million=2_000_000)}
    result=finance.order_contribution(order,[payment],logs,config)
    assert result['cost_micro_usd']==3_001_200 and result['contribution_cents'] is not None


def test_missing_generation_records_never_imply_a_free_video():
    order,payment,_=fixtures()
    result=finance.order_contribution(order,[payment],[],finance.FinanceValues(usd_brl=Decimal('5')))
    assert result['contribution_cents'] is None
    assert 'Sem registros de custo de geração' in result['missing']


@pytest.mark.parametrize('total,count',[(10001,3),(0,3),(-5,2),(999,20)])
def test_revenue_and_fee_allocation_preserves_every_cent(total,count):
    parts=finance.allocate(total,count)
    assert sum(parts)==total and max(parts)-min(parts)<=1


def test_provider_decimal_rounding_and_unknown_fee():
    assert finance.to_cents('0.005')==1
    assert finance.payment_facts({})['fee_cents'] is None
    assert finance.payment_facts({'fee_details':[{}]})['fee_cents'] is None


@pytest.mark.parametrize('path',['/admin/finance','/admin/finance/settings','/admin/customers'])
def test_financial_read_routes_require_administrator(path):
    app=FastAPI();app.include_router(admin_finance.router);app.dependency_overrides[get_session]=lambda:None
    assert TestClient(app).get(path).status_code==401


@pytest.mark.asyncio
async def test_fee_reconciliation_only_writes_auxiliary_facts(monkeypatch):
    from app.db.models import PaymentStatus
    order,payment,_=fixtures();payment.status=PaymentStatus.APPROVED;original=payment.__dict__.copy()
    session=NS(execute=AsyncMock(return_value=NS(scalars=lambda:NS(all=lambda:[payment]))),commit=AsyncMock())
    lookup=AsyncMock(return_value={'id':'123','external_reference':'web_1','currency_id':'BRL','amount':'100.00','fee_details':[{'amount':'3.50'}]})
    record=AsyncMock()
    monkeypatch.setattr(admin_finance.mercadopago_client,'get_payment_details',lookup)
    monkeypatch.setattr(finance,'record_payment_facts',record)
    assert await admin_finance.reconcile_fees(1,session)=={'reconciled':1}
    assert payment.__dict__==original
    record.assert_awaited_once()
