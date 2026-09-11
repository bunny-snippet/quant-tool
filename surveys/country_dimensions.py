"""Small market projections with a scoped, covering MySQL query plan."""

from django.core.exceptions import EmptyResultSet
from django.db import connections
from django.db.models.expressions import Col
from django.db.models.lookups import Lookup
from django.db.models.sql.where import WhereNode


COUNTRY_SCOPE_INDEX = "survey_client_country_cpi_idx"
_COVERED_COLUMNS = {"client_id", "country_code", "country", "cpi"}
_SIMPLE_LOOKUPS = {"exact", "in", "gt", "gte", "lt", "lte", "isnull"}


def _is_covered_client_scope(query):
    """Hint only the measured scalar organization scope, never supplier joins."""
    if query.combinator or query.is_sliced or query.group_by is not None:
        return False
    base_alias = query.get_initial_alias()
    seen_client = False

    def covered(node):
        nonlocal seen_client
        if isinstance(node, WhereNode):
            return all(covered(child) for child in node.children)
        if not isinstance(node, Lookup) or node.lookup_name not in _SIMPLE_LOOKUPS:
            return False
        lhs = node.lhs
        # Organization policies start their OR chain with Q(pk__in=[]).
        # Django removes this always-empty leaf during SQL compilation; it
        # never requires a primary-row lookup or widens the permission scope.
        if (
            isinstance(lhs, Col)
            and lhs.alias == base_alias
            and node.lookup_name == "in"
            and node.rhs_is_direct_value()
            and isinstance(node.rhs, (list, tuple, set, frozenset))
            and not node.rhs
        ):
            return True
        if (
            not isinstance(lhs, Col)
            or lhs.alias != base_alias
            or lhs.target.column not in _COVERED_COLUMNS
            or not node.rhs_is_direct_value()
        ):
            return False
        seen_client = seen_client or lhs.target.column == "client_id"
        return True

    return covered(query.where) and seen_client


def country_dimension_rows(queryset):
    """Return exactly the existing DISTINCT country pairs in database order.

    MySQL can prefer the shorter country/label index to avoid a tiny sort,
    despite needing hundreds of thousands of wide base-row lookups to check
    client/CPI eligibility. For that specific scope use the covering index.
    All SQL predicates, values, DISTINCT and ordering remain ORM-generated.
    Complex/supplier scopes and other database engines keep their usual plan.
    """
    rows = (
        queryset.exclude(country_code="")
        .values_list("country_code", "country")
        .distinct()
        .order_by("country_code")
    )
    if rows.query.is_empty():
        return []
    connection = connections[rows.db]
    if not (
        connection.vendor == "mysql"
        and not connection.mysql_is_mariadb
        and connection.mysql_version >= (8, 0, 20)
        and _is_covered_client_scope(rows.query)
    ):
        return list(rows)

    try:
        sql, params = rows.query.get_compiler(using=rows.db).as_sql()
    except EmptyResultSet:
        # Empty IN predicates can become empty only during SQL compilation.
        return []
    alias = connection.ops.quote_name(rows.query.get_initial_alias())
    index = connection.ops.quote_name(COUNTRY_SCOPE_INDEX)
    # Only a fixed optimizer hint is inserted. Never interpolate filter values
    # or rebuild access predicates; the original bound parameters are retained.
    hinted_sql = sql.replace("SELECT ", f"SELECT /*+ INDEX({alias} {index}) */ ", 1)
    with connection.cursor() as cursor:
        cursor.execute(hinted_sql, params)
        return list(cursor.fetchall())
