from django import template
register = template.Library()

@register.filter
def get_item_by_name(store_items, item_name):
    return store_items.filter(item_name=item_name).first()