from django.core.management.base import BaseCommand
from app.chatbot_retrieval import upsert_intent_mapping
from app.models import ChatbotReviewItem


class Command(BaseCommand):
    help = "Publish developer-approved intent mappings from ChatbotReviewItem into Qdrant intent_collection."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100, help="Max items to publish")

    def handle(self, *args, **options):
        limit = int(options.get("limit") or 100)

        qs = (
            ChatbotReviewItem.objects
            .filter(status="approved", kind="clarification", intent_payload__isnull=False, published_to_intent=False)
            .order_by("created_at")[:limit]
        )

        if not qs:
            self.stdout.write(self.style.SUCCESS("No approved items to publish."))
            return

        count = 0
        for item in qs:
            try:
                payload = item.intent_payload or {}
                pid = upsert_intent_mapping(payload, point_id=item.id)
                item.published_to_intent = True
                item.published_at = item.published_at or item.created_at
                item.qdrant_point_id = str(pid)
                item.save(update_fields=["published_to_intent", "published_at", "qdrant_point_id"])
                count += 1
            except Exception as e:
                item.error = (item.error or "") + f"\nPublish error: {e}"
                item.save(update_fields=["error"])

        self.stdout.write(self.style.SUCCESS(f"Published {count} intent mappings."))
