from collections import deque
from app.config import settings

events_store = deque(maxlen=settings.max_events)
