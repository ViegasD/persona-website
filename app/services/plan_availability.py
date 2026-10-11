"""Storefront availability, independent of historical order access."""

from app.db.models import Plan


RETIRED_PLAN_SLUGS = frozenset({"aniversario"})


def plan_is_available(plan: Plan) -> bool:
    return plan.is_active and plan.slug not in RETIRED_PLAN_SLUGS
