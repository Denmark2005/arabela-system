from django.apps import AppConfig


class GownsConfig(AppConfig):
    name = 'gowns'

    def ready(self):
        # Adding or removing a category during a page visit must be seen by the rest of that visit at once: wipe the
        # visit's memory of the category lists (gowns/request_memo.py).
        from django.db.models.signals import post_delete, post_save

        from gowns.models import CustomCategory, HiddenCategory
        from gowns.request_memo import forget_all
        for model in (CustomCategory, HiddenCategory):
            post_save.connect(forget_all, sender=model, dispatch_uid=f"memo_forget_save_{model.__name__}")
            post_delete.connect(forget_all, sender=model, dispatch_uid=f"memo_forget_delete_{model.__name__}")
