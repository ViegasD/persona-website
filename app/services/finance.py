"""Cash contribution by order/video. Unknown costs stay unknown; money uses integers/Decimal."""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import UTC, datetime
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from app.db.models import FinanceSettings, OrderFinanceNote, PaymentFinanceSnapshot


class ModelRate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_micro_usd_per_million: int = Field(ge=0, le=1_000_000_000, strict=True)
    output_micro_usd_per_million: int = Field(ge=0, le=1_000_000_000, strict=True)


class FinanceValues(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    revision: int = Field(0,ge=0)
    usd_brl: Decimal | None = Field(None,gt=0,le=100,decimal_places=6)
    estimated_fee_bps: int | None = Field(None,ge=0,le=10000,strict=True)
    estimated_fee_fixed_cents: int = Field(0,ge=0,le=100000,strict=True)
    overhead_per_video_cents: int = Field(0,ge=0,le=100000,strict=True)
    model_rates: dict[str,ModelRate] = Field(default_factory=dict,max_length=30)


class NoteValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_test: StrictBool = False
    note: str | None = Field(None,max_length=2000)
    fee_override_cents: int | None = Field(None,ge=0,le=10000000,strict=True)
    additional_cost_cents: int = Field(0,ge=0,le=10000000,strict=True)


def money(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"),rounding=ROUND_HALF_UP))


def to_cents(value) -> int | None:
    try:
        d=Decimal(str(value))
        return money(d*100) if d.is_finite() and d>=0 else None
    except (InvalidOperation,ValueError,TypeError): return None


def allocate(total: int, count: int) -> list[int]:
    if count<=0:return []
    q,r=divmod(total,count)
    return [q+(1 if i<r else 0) for i in range(count)]


async def settings(session) -> FinanceValues:
    row=await session.get(FinanceSettings,1)
    return FinanceValues.model_validate(row) if row else FinanceValues()


async def save_settings(session,values: FinanceValues) -> FinanceValues:
    await session.execute(insert(FinanceSettings).values(id=1).on_conflict_do_nothing())
    row=(await session.execute(select(FinanceSettings).where(FinanceSettings.id==1).with_for_update()
                               .execution_options(populate_existing=True))).scalar_one()
    if row.revision!=values.revision:raise HTTPException(409,"Configurações alteradas; recarregue a página.")
    for key,value in values.model_dump(exclude={"revision"}).items():setattr(row,key,value)
    row.revision+=1;row.updated_at=datetime.now(UTC)
    await session.commit();return FinanceValues.model_validate(row)


def payment_facts(details: dict) -> dict:
    """Parse only explicit provider fee/refund values, never assume missing fees are zero."""
    fees=details.get("fee_details")
    fee=None
    if isinstance(fees,list):
        amounts=[to_cents(f.get("amount")) for f in fees if isinstance(f,dict)]
        if len(amounts)==len(fees) and all(v is not None for v in amounts):fee=sum(amounts)
    return {"fee_cents":fee,"refunded_cents":to_cents(details.get("amount_refunded",0)) or 0,
            "net_received_cents":to_cents(details.get("net_received_amount"))}


async def record_payment_facts(session,payment,details):
    facts=payment_facts(details)
    await session.execute(insert(PaymentFinanceSnapshot).values(payment_id=payment.id,**facts)
                          .on_conflict_do_update(index_elements=["payment_id"],set_={**facts,"updated_at":datetime.now(UTC)}))


def cost_amount(log,config: FinanceValues) -> tuple[int,bool]:
    if getattr(log,"cost_source","estimated")!="usage_unpriced":return log.cost_micro_usd,True
    rate=config.model_rates.get(getattr(log,"model",None))
    usage=getattr(log,"usage",None) or {}
    if not rate:return 0,False
    value=(Decimal(usage.get("input_tokens",0))*rate.input_micro_usd_per_million
           +Decimal(usage.get("output_tokens",0))*rate.output_micro_usd_per_million)/1_000_000
    return money(value),True


def order_contribution(order,payments,logs,config: FinanceValues,note=None,snapshots=None,commission_pct=Decimal(0)) -> dict:
    snapshots=snapshots or {}
    state=lambda v:getattr(v,"value",v)
    accepted=[p for p in payments if state(p.status) in {"APPROVED","REFUNDED"}]
    funding=[p for p in accepted if p.funds_order]
    # Older data can lack the funding flag. A single approved record is unambiguous.
    selected=funding or (accepted if len(accepted)==1 else [])
    gross=sum(p.amount_cents for p in selected)
    refunds=0;fee=0;missing=[];fee_source="provider"
    if not selected and state(order.status) not in {"DRAFT","AWAITING_PAYMENT"}:missing.append("Pagamento sem comprovação financeira")
    if len(accepted)>len(selected):missing.append("Pagamentos adicionais precisam de revisão")
    override=getattr(note,"fee_override_cents",None)
    for p in selected:
        snap=snapshots.get(p.id)
        facts=payment_facts(p.raw_webhook or {})
        refunded=(snap.refunded_cents if snap else facts["refunded_cents"])
        if state(p.status)=="REFUNDED" or state(order.status)=="REFUNDED":refunded=p.amount_cents
        refunds+=min(p.amount_cents,refunded)
        current=snap.fee_cents if snap else facts["fee_cents"]
        if p.provider=="credits":current=0
        if current is not None:fee+=current
        elif override is not None:pass
        elif config.estimated_fee_bps is not None:
            fee+=money(Decimal(p.amount_cents)*config.estimated_fee_bps/10000)+config.estimated_fee_fixed_cents
            fee_source="estimated"
        else:missing.append("Taxa de pagamento não conciliada")
    if override is not None:fee=override;fee_source="manual"
    items=sorted(order.items,key=lambda i:i.sequence)
    count=len(items)
    allocations=allocate(gross-refunds,count)
    fees=allocate(fee,count)
    commissions=allocate(money(Decimal(max(0,gross-refunds))*commission_pct/100),count)
    additional=allocate(getattr(note,"additional_cost_cents",0) or 0,count)
    by_item={i.id:[] for i in items}
    for log in logs:
        if log.order_item_id in by_item:by_item[log.order_item_id].append(log)
    rows=[]
    for idx,item in enumerate(items):
        current=by_item[item.id];reasons=list(missing)
        amounts=[cost_amount(log,config) for log in current]
        usd=sum(v for v,_ in amounts)
        if not current:reasons.append("Sem registros de custo de geração")
        if current and not any(log.operation=="video_generation" for log in current):reasons.append("Custo do vídeo não registrado")
        if any(not known for _,known in amounts):reasons.append("Preço de modelo de IA não configurado")
        if config.usd_brl is None:reasons.append("Câmbio USD/BRL não configurado")
        cost_brl=money(Decimal(usd)*config.usd_brl/10000) if config.usd_brl is not None else None
        recognized=bool(selected)
        profit=(allocations[idx]-fees[idx]-commissions[idx]-cost_brl-config.overhead_per_video_cents-additional[idx]
                if recognized and not reasons and cost_brl is not None else None)
        rows.append({"item_id":item.id,"sequence":item.sequence,"status":state(item.status),"characters":item.character_ids,
                     "revenue_cents":allocations[idx],"fee_cents":fees[idx] if not any("Taxa" in r for r in reasons) else None,
                     "commission_cents":commissions[idx],"cost_micro_usd":usd,"cost_brl_cents":cost_brl,
                     "overhead_cents":config.overhead_per_video_cents+additional[idx],"contribution_cents":profit,
                     "cost_records":len(current),"video_attempts":sum(l.operation=="video_generation" for l in current),
                     "estimated":any(getattr(l,"cost_source","estimated")!="provider" for l in current) or fee_source!="provider" or commission_pct>0,
                     "missing":list(dict.fromkeys(reasons))})
    complete=bool(rows) and all(row["contribution_cents"] is not None for row in rows)
    return {"order_id":order.id,"status":state(order.status),"created_at":order.created_at,"paid_at":order.paid_at,
            "customer":order.recipient_name or order.guest_email or "Sem nome","email":order.guest_email,
            "expected_cents":order.total_cents,"gross_cents":gross,"refunded_cents":refunds,"net_revenue_cents":gross-refunds,
            "fee_cents":fee if not any("Taxa" in r for r in missing) else None,"fee_source":fee_source,
            "is_test":bool(getattr(note,"is_test",False)),"note":getattr(note,"note",None),
            "additional_cost_cents":getattr(note,"additional_cost_cents",0) or 0,"fee_override_cents":override,
            "cost_micro_usd":sum(r["cost_micro_usd"] for r in rows),
            "cost_brl_cents":sum(r["cost_brl_cents"] for r in rows) if rows and all(r["cost_brl_cents"] is not None for r in rows) else None,
            "contribution_cents":sum(r["contribution_cents"] for r in rows) if complete else None,
            "has_payment":bool(selected),"items":rows,"missing":list(dict.fromkeys(r for row in rows for r in row["missing"]))}
