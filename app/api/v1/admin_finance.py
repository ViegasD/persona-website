"""Operational reporting and isolated financial annotations; never reprocess payments/orders."""
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from app.api.deps import require_admin_key
from app.db.session import get_session
from app.db.models import (Order,OrderItem,OrderStatus,User,ApiCostLog,Payment,Affiliate,OrderFinanceNote,PaymentFinanceSnapshot)
from app.services import finance,mercadopago_client

router=APIRouter(prefix="/admin",tags=["admin-finance"],dependencies=[Depends(require_admin_key)])


@router.get('/finance/settings')
async def get_finance_settings(session=Depends(get_session)):
    return await finance.settings(session)


@router.put('/finance/settings')
async def put_finance_settings(values:finance.FinanceValues,session=Depends(get_session)):
    return await finance.save_settings(session,values)


@router.put('/orders/{order_id}/finance-note')
async def save_note(order_id:int,values:finance.NoteValues,session=Depends(get_session)):
    if not await session.get(Order,order_id):raise HTTPException(404,"Pedido não encontrado")
    from sqlalchemy.dialects.postgresql import insert
    data=values.model_dump()
    await session.execute(insert(OrderFinanceNote).values(order_id=order_id,**data)
                          .on_conflict_do_update(index_elements=['order_id'],set_={**data,'updated_at':datetime.now(UTC)}))
    await session.commit();return data


@router.post('/orders/{order_id}/reconcile-fees')
async def reconcile_fees(order_id:int,session=Depends(get_session)):
    rows=(await session.execute(select(Payment).where(Payment.order_id==order_id,Payment.provider=='mercadopago',
                       Payment.provider_id.is_not(None)))).scalars().all()
    if not rows:raise HTTPException(409,"Nenhum pagamento Mercado Pago para conciliar")
    # Provider GET only. Do not call webhook handlers or change historical payment/order state.
    count=0
    for payment in rows:
        if payment.status.value not in {'APPROVED','REFUNDED'}:continue
        try:details=await mercadopago_client.get_payment_details(payment.provider_id)
        except Exception:raise HTTPException(502,"Não foi possível consultar o provedor; nenhum pedido foi alterado.") from None
        if (details.get('external_reference')!=f'web_{order_id}' or details.get('currency_id')!='BRL'
                or finance.to_cents(details.get('amount'))!=payment.amount_cents):
            raise HTTPException(409,"Dados do provedor não correspondem a este pagamento")
        await finance.record_payment_facts(session,payment,details);count+=1
    await session.commit();return {'reconciled':count}


@router.get('/finance')
async def report(date_from:date|None=None,date_to:date|None=None,q:str=Query('',max_length=100),
                 status:OrderStatus|None=None,include_tests:bool=False,order_id:int|None=None,
                 attention:bool=False,limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0),session=Depends(get_session)):
    if date_from and date_to and date_from>date_to:raise HTTPException(422,"Período inválido")
    stmt=select(Order).options(selectinload(Order.items),selectinload(Order.payments),selectinload(Order.plan))
    if order_id is not None:stmt=stmt.where(Order.id==order_id)
    else:
        tz=ZoneInfo('America/Araguaina');stamp=func.coalesce(Order.paid_at,Order.created_at)
        if date_from:stmt=stmt.where(stamp>=datetime.combine(date_from,time(),tzinfo=tz))
        if date_to:stmt=stmt.where(stamp<datetime.combine(date_to+timedelta(days=1),time(),tzinfo=tz))
        if status:stmt=stmt.where(Order.status==status)
        if q:
            escaped=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            matches=[Order.recipient_name.ilike(f'%{escaped}%',escape='\\'),Order.guest_email.ilike(f'%{escaped}%',escape='\\')]
            if q.lstrip('#').isdigit():matches.append(Order.id==int(q.lstrip('#')))
            stmt=stmt.where(or_(*matches))
    orders=(await session.execute(stmt.order_by(Order.created_at.desc()).limit(5001))).scalars().all()
    truncated=len(orders)>5000;orders=orders[:5000]
    ids=[o.id for o in orders];item_ids=[i.id for o in orders for i in o.items]
    notes={n.order_id:n for n in (await session.execute(select(OrderFinanceNote).where(OrderFinanceNote.order_id.in_(ids)))).scalars()}
    logs=(await session.execute(select(ApiCostLog).where(ApiCostLog.order_item_id.in_(item_ids)))).scalars().all()
    by_item={}
    for log in logs:by_item.setdefault(log.order_item_id,[]).append(log)
    payment_ids=[p.id for o in orders for p in o.payments]
    snapshots={p.payment_id:p for p in (await session.execute(select(PaymentFinanceSnapshot).where(PaymentFinanceSnapshot.payment_id.in_(payment_ids)))).scalars()}
    affiliate_ids={o.affiliate_id for o in orders if o.affiliate_id}
    rates={a.id:a.commission_pct for a in (await session.execute(select(Affiliate).where(Affiliate.id.in_(affiliate_ids)))).scalars()}
    config=await finance.settings(session);result=[];excluded=0
    from app.services.processing_policy import processing_allowed
    for order in orders:
        note=notes.get(order.id)
        if note and note.is_test and not include_tests and order_id is None:excluded+=1;continue
        row=finance.order_contribution(order,order.payments,[l for i in order.items for l in by_item.get(i.id,[])],
                                      config,note,snapshots,rates.get(order.affiliate_id,Decimal(0)))
        row['plan']=order.plan.name if order.plan else None
        row['quality']=order.quality
        row['processing_allowed']=processing_allowed(order)
        row['error']=bool(order.error or any(i.error for i in order.items))
        row['needs_attention']=row['error'] or (row['has_payment'] and row['status'] not in {'READY','DELIVERED','REFUNDED'})
        if attention and not row['needs_attention']:continue
        result.append(row)
    paid=[r for r in result if r['has_payment']]
    known=[r for r in paid if r['contribution_cents'] is not None]
    statuses={}
    for row in result:statuses[row['status']]=statuses.get(row['status'],0)+1
    return {'items':result[offset:offset+limit],'total':len(result),'truncated':truncated,'settings':config,
            'summary':{'orders':len(result),'paid_orders':len(paid),'delivered':sum(r['status']=='DELIVERED' for r in result),
                       'attention':sum(r['needs_attention'] for r in result),'excluded_tests':excluded,'statuses':statuses,
                       'gross_cents':sum(r['gross_cents'] for r in result),'refunded_cents':sum(r['refunded_cents'] for r in result),
                       'net_revenue_cents':sum(r['net_revenue_cents'] for r in result),
                       'fee_cents':sum(r['fee_cents'] for r in paid) if all(r['fee_cents'] is not None for r in paid) else None,
                       'cost_micro_usd':sum(r['cost_micro_usd'] for r in result),
                       'cost_brl_cents':sum(r['cost_brl_cents'] for r in result) if result and all(r['cost_brl_cents'] is not None for r in result) else None,
                       'contribution_cents':sum(r['contribution_cents'] for r in known) if paid and len(known)==len(paid) else None,
                       'calculated_orders':len(known),'partial_contribution_cents':sum(r['contribution_cents'] for r in known),
                       'unassigned_cost_records':(await session.execute(select(func.count(ApiCostLog.id)).where(ApiCostLog.order_item_id.is_(None)))).scalar_one()}}


@router.get('/customers')
async def customers(q:str=Query('',max_length=100),offset:int=Query(0,ge=0),session=Depends(get_session)):
    stmt=select(User).order_by(User.created_at.desc())
    if q:stmt=stmt.where(User.email.ilike('%'+q.replace('%','\\%').replace('_','\\_')+'%',escape='\\'))
    users=(await session.execute(stmt.offset(offset).limit(50))).scalars().all()
    counts=dict((await session.execute(select(Order.user_id,func.count(Order.id)).where(Order.user_id.in_([u.id for u in users])).group_by(Order.user_id))).all())
    return [{'id':u.id,'email':u.email,'name':u.name,'verified':bool(u.email_verified_at),'credits':u.credit_balance,
             'orders':counts.get(u.id,0),'created_at':u.created_at} for u in users]
