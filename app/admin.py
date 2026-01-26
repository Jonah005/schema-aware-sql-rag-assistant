from django.contrib import admin

from .models import ChatbotReviewItem


@admin.register(ChatbotReviewItem)
class ChatbotReviewItemAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "user", "kind", "status", "published_to_intent")
    list_filter = ("kind", "status", "published_to_intent", "created_at")
    search_fields = ("question",)
    readonly_fields = ("created_at", "updated_at")
