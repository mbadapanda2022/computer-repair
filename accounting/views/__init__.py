# accounting/views/__init__.py

# ============================================================
# STAFF VIEWS (all staff views – default names)
# ============================================================
from .dashboard import (
    dashboard as staff_dashboard,
    dashboard_stats as staff_dashboard_stats,
    recent_transactions as staff_recent_transactions,
    refresh_stats,
    dashboard_export,
)

from .auth import *
from .bank import *
from .contacts import *
from .contact_messages import *
from .error_handlers import *
from .journal import *
from .notifications import *
from .payments import *
from .products import *
from .purchases import *
from .repairs import *
from .reports import *
from .sales import *
from .settings import *
from .statements import *
from .stock import *

# ============================================================
# CUSTOMER VIEWS (with customer_ prefix)
# ============================================================
from .customer_views import (
    dashboard as customer_dashboard,
    invoice_list as customer_invoice_list,
    invoice_detail as customer_invoice_detail,
    invoice_print as customer_invoice_print,
    repair_list as customer_repair_list,
    repair_detail as customer_repair_detail,
    repair_print as customer_repair_print,
    payment_list as customer_payment_list,
    statement as customer_statement,
    profile as customer_profile,
    profile_update as customer_profile_update,
    repair_create as customer_repair_create,
    repair_update as customer_repair_update,
    repair_delete as customer_repair_delete,
    repair_estimate_approve as customer_repair_estimate_approve,
    repair_estimate_hold as customer_repair_estimate_hold,
    repair_estimate_reject as customer_repair_estimate_reject,
    validate_repair_field as customer_validate_repair_field,
)

# ============================================================
# LANDING VIEWS (public – direct from landing_views module )
# ============================================================
from .landing_views import *

# ============================================================
# DEFAULT EXPORTS (using for staff URLs )
# ============================================================
dashboard = staff_dashboard
dashboard_stats = staff_dashboard_stats
recent_transactions = staff_recent_transactions