from django.contrib import admin

from .models import Gown


@admin.register(Gown)
class GownAdmin(admin.ModelAdmin):
    list_display = ('gown_id', 'name', 'slug', 'category', 'color_name', 'size', 'rental_price', 'condition', 'status', 'is_verified')
    list_filter = ('category', 'status', 'condition', 'size')
    search_fields = ('gown_id', 'name', 'color_name', 'slug')
    readonly_fields = ('slug',)
