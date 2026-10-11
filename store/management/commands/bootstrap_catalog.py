"""Load the reviewed catalog only into an otherwise product-empty database."""
from pathlib import Path

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connection, transaction

from store.models import Product


# A stable advisory lock prevents two autoscaled web instances from both trying
# to seed a freshly-created PostgreSQL database. It is intentionally used only
# for the short, first-boot bootstrap operation.
CATALOG_BOOTSTRAP_LOCK = 735981247139206421
FIXTURE_NAME = "production_catalog_seed.json"


class Command(BaseCommand):
    help = "Load the reviewed production catalog when no products exist."

    def _load_if_needed(self):
        if Product.objects.exists():
            self.stdout.write("Catalog bootstrap skipped: products already exist.")
            return

        fixture_path = Path(settings.BASE_DIR) / "data" / FIXTURE_NAME
        if not fixture_path.is_file():
            raise CommandError(f"Required catalog fixture is missing: {fixture_path}")

        self.stdout.write("Loading initial ShopEase catalog fixture.")
        call_command("loaddata", str(fixture_path), verbosity=self.verbosity)
        product_count = Product.objects.count()
        if not product_count:
            raise CommandError("Catalog fixture completed without creating products.")
        self.stdout.write(self.style.SUCCESS(f"Catalog bootstrap complete: {product_count} products."))

    def handle(self, *args, **options):
        self.verbosity = options.get("verbosity", 1)

        if connection.vendor != "postgresql":
            with transaction.atomic():
                self._load_if_needed()
            return

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [CATALOG_BOOTSTRAP_LOCK])
        try:
            with transaction.atomic():
                self._load_if_needed()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [CATALOG_BOOTSTRAP_LOCK])
