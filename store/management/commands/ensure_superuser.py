"""Create or promote a production administrator from secret-backed settings."""
import os

from django.contrib.auth import get_user_model
from django.core.management import BaseCommand, CommandError
from django.db import transaction


def _enabled(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Command(BaseCommand):
    help = "Ensure the configured Django superuser exists without publishing credentials."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip().lower()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        reset_password = _enabled("RESET_SUPERUSER_PASSWORD", False)

        if not username or not email or not password:
            raise CommandError(
                "DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_EMAIL, and "
                "DJANGO_SUPERUSER_PASSWORD are required when CREATE_SUPERUSER=true."
            )

        User = get_user_model()
        with transaction.atomic():
            user = (
                User.objects.select_for_update().filter(username=username).first()
                or User.objects.select_for_update().filter(email__iexact=email).first()
            )
            created = user is None
            if created:
                user = User(username=username, email=email)
                user.set_password(password)
            else:
                user.email = email
                if reset_password:
                    user.set_password(password)

            user.is_active = True
            user.is_staff = True
            user.is_superuser = True
            user.save()

        action = "created" if created else "verified"
        self.stdout.write(self.style.SUCCESS(f"Production superuser {action}."))
