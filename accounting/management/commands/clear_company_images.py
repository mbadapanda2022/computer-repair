# accounting/management/commands/clear_company_images.py
from django.core.management.base import BaseCommand
from accounting.models import CompanyProfile

class Command(BaseCommand):
    help = 'Clear logo, hero_image, og_image from CompanyProfile'

    def handle(self, *args, **options):
        company = CompanyProfile.get_instance()
        
        # Delete old files from storage (Cloudinary)
        if company.logo:
            self.stdout.write(f"Deleting logo: {company.logo.name}")
            company.logo.delete(save=False)
            company.logo = None
        
        if company.hero_image:
            self.stdout.write(f"Deleting hero_image: {company.hero_image.name}")
            company.hero_image.delete(save=False)
            company.hero_image = None
        
        if company.og_image:
            self.stdout.write(f"Deleting og_image: {company.og_image.name}")
            company.og_image.delete(save=False)
            company.og_image = None
        
        company.save()
        self.stdout.write(self.style.SUCCESS('✅ All images cleared successfully!'))