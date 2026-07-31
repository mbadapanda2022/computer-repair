from django.apps import AppConfig


class AccountingConfig(AppConfig):
    name = 'accounting'
    
    def ready(self):
        # Import signals so they get registered
        import accounting.signals
