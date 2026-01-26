# Generated to remove pgvector dependency.
# This project uses Qdrant for embeddings, so we store schema embeddings as bytes (optional).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SchemaEmbedding",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("table_name", models.CharField(max_length=255, db_index=True)),
                ("chunk_text", models.TextField()),
                ("embedding", models.BinaryField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
