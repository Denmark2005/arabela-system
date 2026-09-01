from django.contrib import admin

from .models import Reservation, ReservationItem


class ReservationItemInline(admin.TabularInline):
    model = ReservationItem
    extra = 1
    fields = ('gown_name', 'gown', 'size', 'rental_price', 'rental_date', 'return_date',
              'stage', 'picked_up_on', 'returned_on')
    autocomplete_fields = ('gown',)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ('reference_code', 'customer_name', 'status', 'total_amount', 'created_at')
    list_filter = ('status', 'payment_method', 'created_at')
    search_fields = ('reference_code', 'customer_name', 'customer__email')
    readonly_fields = ('reference_code', 'created_at', 'updated_at')
    inlines = [ReservationItemInline]
