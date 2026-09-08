"""Role-controlled aggregate leaderboard; never expands tracking/report access."""
from django.db.models import Count, Q
from accounts.models import EmployeeProfile
from accounts.profile_context import employee_profile_for_user
from vendors.access import organization_unit_descendant_ids
from .models import SurveyAttempt


def configured_performers(queryset, user, card_access, total_completes, window):
    from .dashboard import _top_suppliers, COMPLETED
    profile = employee_profile_for_user(user)
    policy = profile.role.dashboard_performers if profile and profile.role and profile.role.is_active else {}
    mode = policy.get("mode", "mixed")
    if mode == "team":
        # Opt-in policy configured by owner. An employee's exact assigned unit
        # (shift/sub-branch/branch) is the maximum peer leaderboard boundary.
        unit = profile.organization_unit if profile else None
        if not unit or profile.account_type != EmployeeProfile.AccountType.EMPLOYEE:
            queryset = queryset.filter(platform_user_id=user.pk)
        else:
            members = EmployeeProfile.objects.filter(
                organization_unit_id__in=organization_unit_descendant_ids(unit),
                organization_unit__workspace_owner_id=unit.workspace_owner_id,
                account_type=EmployeeProfile.AccountType.EMPLOYEE,
            ).values("user_id")
            queryset = SurveyAttempt.objects.filter(platform_user_id__in=members, initiated_at__gte=window["start"], initiated_at__lt=window["end"])
    else:
        if policy.get("supplier_ids"):
            queryset = queryset.filter(vendor_id__in=policy["supplier_ids"])
        if policy.get("branch_ids"):
            path = "platform_user__employee_profile__organization_unit"
            ids = policy["branch_ids"]
            queryset = queryset.filter(Q(**{path+"__id__in":ids}) | Q(**{path+"__parent_id__in":ids}) | Q(**{path+"__parent__parent_id__in":ids}))
        if mode == "suppliers":
            queryset = queryset.filter(vendor__employee_profile__account_type__in=["internal_vendor", "external_vendor"])
    if mode in {"users", "team"}:
        rows = queryset.order_by().values("platform_user_id", "platform_user__first_name", "platform_user__last_name", "platform_user__username").annotate(completes=Count("id", filter=Q(status=COMPLETED))).filter(completes__gt=0).order_by("-completes", "platform_user_id")[:8]
        return [{"name":" ".join(filter(None,[r["platform_user__first_name"],r["platform_user__last_name"]])).strip() or r["platform_user__username"] or "Deleted user", "branch_name":"", "completes":r["completes"]} for r in rows]
    if mode == "suppliers":
        rows = queryset.order_by().values("vendor_id", "vendor__first_name", "vendor__last_name", "vendor__username", "vendor__employee_profile__company_name").annotate(completes=Count("id",filter=Q(status=COMPLETED))).filter(completes__gt=0).order_by("-completes","vendor_id")[:8]
        return [{"name":r["vendor__employee_profile__company_name"] or " ".join(filter(None,[r["vendor__first_name"],r["vendor__last_name"]])).strip() or r["vendor__username"], "branch_name":"", "completes":r["completes"]} for r in rows]
    return _top_suppliers(queryset, user, card_access, total_completes)
