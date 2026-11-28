from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def signed_peso(amount):
    """An amount written as text ("1,500.00" or "-2,300.00") with the peso sign, the minus in front of it: more can be handed back than received."""
    text = str(amount)
    if text.startswith("-"):
        return mark_safe("&minus;&#8369;" + escape(text[1:]))
    return mark_safe("&#8369;" + escape(text))
