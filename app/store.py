from collections import deque
from app.config import MAX_EVENTS

events_store = deque(maxlen=MAX_EVENTS)