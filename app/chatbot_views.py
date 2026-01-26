import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .chatbot_engine import answer_user_question


@login_required
@ensure_csrf_cookie
def chat_page(request):
    """
    Serves the chat UI and ensures a session exists (used for per-user/per-session Redis keys).
    ensure_csrf_cookie sets the csrftoken cookie so JS can POST safely.
    """
    if not request.session.session_key:
        request.session.save()
    return render(request, "chat/chat.html")


@login_required
@require_POST
def chat_api(request):
    """
    JSON API:
      POST /api/chat/  body: {"message": "...", "conversation_id": "...optional..."}

    Notes:
    - CSRF protection is ON (do NOT exempt).
    - The frontend must send X-CSRFToken header (from csrftoken cookie).
    """
    if not request.session.session_key:
        request.session.save()

    # optional: allow frontend to pass conversation_id, but we still bind to the
    # authenticated user's session for isolation/security
    session_key = request.session.session_key

    # Parse body
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"reply": "Invalid JSON body."}, status=400)

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"reply": "Please type a message."}, status=400)

    try:
        reply = answer_user_question(request.user, message, session_key=session_key)
        return JsonResponse({"reply": reply, "conversation_id": session_key})
    except ValueError as e:
        # expected validation errors
        return JsonResponse({"reply": str(e)}, status=400)
    except Exception:
        # don't leak internals to the UI; check server logs/admin review items instead
        return JsonResponse(
            {"reply": "I hit an error while contacting the server. Check server logs."},
            status=500,
        )
