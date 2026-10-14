"""Load the reviewed catalog only into an otherwise product-empty database."""
import collections
import json
import tempfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connection, transaction

from store.models import Product


# A stable advisory lock prevents two autoscaled web instances from both trying
# to seed a freshly-created PostgreSQL database. It is intentionally used only
# for the short, first-boot bootstrap operation.
CATALOG_BOOTSTRAP_LOCK = 735981247139206421
FIXTURE_NAME = "production_catalog_seed.json"


def unresolved_references(row, available):
    """List the foreign keys of one fixture row that the fixture cannot satisfy."""
    try:
        model = apps.get_model(row["model"])
    except LookupError:
        return []

    missing = []
    for field in model._meta.concrete_fields:
        if not field.is_relation or field.primary_key:
            continue
        value = row["fields"].get(field.name)
        if value is None:
            continue
        target = field.remote_field.model._meta.label_lower
        if value not in available.get(target, ()):
            missing.append(field.name)
    return missing


def prune_unresolved_rows(objects):
    """Split fixture rows into the loadable ones and those with missing parents.

    A seed exported from a live database can reference rows the export filtered
    out, for example product variants whose products are absent.  ``loaddata``
    aborts on the first such row, which would take first-boot seeding down with
    it, so unresolvable rows are removed here and reported by the caller.  The
    loop repeats until it stabilises because removing a parent can orphan the
    rows that referenced it.
    """
    kept, dropped = list(objects), []
    while True:
        available = collections.defaultdict(set)
        for row in kept:
            available[row["model"]].add(row["pk"])

        survivors, removed = [], []
        for row in kept:
            if unresolved_references(row, available):
                removed.append(row)
            else:
                survivors.append(row)

        if not removed:
            return kept, dropped
        dropped.extend(removed)
        kept = survivors


class Command(BaseCommand):
    help = "Load the reviewed production catalog when no products exist."

    def _load_if_needed(self):
        if Product.objects.exists():
            self.stdout.write("Catalog bootstrap skipped: products already exist.")
            return

        fixture_path = Path(settings.BASE_DIR) / "data" / FIXTURE_NAME
        if not fixture_path.is_file():
            raise CommandError(f"Required catalog fixture is missing: {fixture_path}")

        objects = json.loads(fixture_path.read_text(encoding="utf-8"))
        loadable, dropped = prune_unresolved_rows(objects)
        if dropped:
            summary = ", ".join(
                f"{model}={count}"
                for model, count in sorted(collections.Counter(row["model"] for row in dropped).items())
            )
            self.stdout.write(
                self.style.WARNING(
                    f"Skipping {len(dropped)} fixture rows with unresolved references ({summary})."
                )
            )

        self.stdout.write("Loading initial ShopEase catalog fixture.")
        if not dropped:
            call_command("loaddata", str(fixture_path), verbosity=self.verbosity)
        else:
            # loaddata accepts an absolute fixture path, so the pruned rows never
            # have to be written back into the reviewed fixture on disk.
            with tempfile.TemporaryDirectory() as temp_dir:
                pruned_path = Path(temp_dir) / FIXTURE_NAME
                pruned_path.write_text(json.dumps(loadable, indent=1), encoding="utf-8")
                call_command("loaddata", str(pruned_path), verbosity=self.verbosity)

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
