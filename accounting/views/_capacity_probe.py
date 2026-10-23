"""Throwaway read-only capacity probe view. Mount, query on production,
download JSON, then delete this file and the URL wiring."""
import traceback

from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import JsonResponse


@staff_member_required
def db_capacity(request):
    out = {'errors': []}
    try:
        with connection.cursor() as c:
            c.execute("SELECT pg_database_size(current_database())")
            db_bytes = c.fetchone()[0]
            out['db_bytes'] = db_bytes
            out['db_mb'] = round(db_bytes / 1048576, 2)

            c.execute("""
                SELECT relname, pg_total_relation_size(relid)::bigint AS bytes
                FROM pg_stat_user_tables
                ORDER BY pg_total_relation_size(relid) DESC
                LIMIT 40
            """)
            tables = c.fetchall()
            out['tables'] = tables

            counts = {}
            for t, _b in tables[:15]:
                try:
                    c.execute('SELECT count(*) FROM "%s"'
                              % t.replace('"', '""'))
                    counts[t] = c.fetchone()[0]
                except Exception as e:
                    connection.close()
                    c = connection.cursor()
                    counts[t] = None
                    out['errors'].append('count %s: %s' % (t, e))
            out['counts'] = counts

            growth = {}
            for t in list(counts.keys()):
                try:
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
                    c.execute("""
                        SELECT "%s"::date d, count(*) FROM "%s"
                        WHERE "%s" >= now() - interval '30 days'
                        GROUP BY 1 ORDER BY 1 DESC LIMIT 31
                    """ % (col.replace('"', '""'), t.replace('"', '""'),
                           col.replace('"', '""')))
                    growth[t] = c.fetchall()
                except Exception as e:
                    connection.close()
                    c = connection.cursor()
                    out['errors'].append('growth %s: %s' % (t, e))
            out['growth'] = {
                k: [[str(d), n] for d, n in v] for k, v in growth.items()}
    except Exception:
        out['fatal'] = traceback.format_exc()
    return JsonResponse(out)
