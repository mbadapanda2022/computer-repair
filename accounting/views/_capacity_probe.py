"""Throwaway read-only capacity probe view. Mount, query on production,
download JSON, then delete this file and the URL wiring."""
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import JsonResponse


def _fetchall(c, sql):
    c.execute(sql)
    return c.fetchall()


@staff_member_required
def db_capacity(request):
    with connection.cursor() as c:
        db_bytes = _fetchall(
            c, "SELECT pg_database_size(current_database())")[0][0]

        tables = _fetchall(c, """
            SELECT relname, pg_total_relation_size(relid)::bigint AS bytes
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(relid) DESC
            LIMIT 40
        """)

        counts = {}
        for t, _b in tables[:15]:
            counts[t] = _fetchall(
                c, 'SELECT count(*) FROM "%s"' % t.replace('"', '""'))[0][0]

        # per-day insert counts (last 30 days) for tables that have a
        # timestamp-ish column
        growth = {}
        for t in list(counts.keys()):
            c.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s
                  AND data_type IN ('timestamp with time zone',
                                    'timestamp without time zone')
                ORDER BY column_name LIMIT 1
            """, [t])
            row = c.fetchone()
            if not row:
                continue
            col = row[0]
            growth[t] = _fetchall(c, """
                SELECT "%s"::date d, count(*) FROM "%s"
                WHERE "%s" >= now() - interval '30 days'
                GROUP BY 1 ORDER BY 1 DESC LIMIT 31
            """ % (col.replace('"', '""'), t.replace('"', '""'),
                   col.replace('"', '""')))

    return JsonResponse({
        'db_bytes': db_bytes,
        'db_mb': round(db_bytes / 1048576, 2),
        'tables': tables,
        'counts': counts,
        'growth': {k: [[str(d), n] for d, n in v] for k, v in growth.items()},
    })
