from django.apps import AppConfig


class PackInstallerConfig(AppConfig):
    name = "packinstaller"
    verbose_name = "PackInstaller"
    default_auto_field = "django.db.models.BigAutoField"
    dpy_package = "packinstaller.packinstaller"
