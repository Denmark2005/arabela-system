"""A memory that lasts for ONE page visit only, so a question the page asks many times is sent to the database once.

The category lists ("which built-in categories did the owner remove?", "which categories did the owner add?") are read by the
menu, the search box, the tiles and the page itself -- up to 15 times on one page, each a trip to the database. Inside a visit
the answer cannot change unless the visit itself adds or removes a category, and that wipes the memory at once (gowns/apps.py).

Nothing is kept between visits: RequestMemoMiddleware starts every visit with an empty memory and throws it away at the end. Code
that runs outside a page visit (management commands, tests calling a function directly) has no memory and asks every time, as before."""
import contextvars

_memo = contextvars.ContextVar("arabela_request_memo", default=None)


class RequestMemoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _memo.set({})
        try:
            return self.get_response(request)
        finally:
            _memo.reset(token)


def remember(key, compute):
    """compute() once per visit for this key; outside a visit, every time."""
    store = _memo.get()
    if store is None:
        return compute()
    if key not in store:
        store[key] = compute()
    return store[key]


def forget_all(*args, **kwargs):
    """Wipes this visit's memory (connected to category saves/deletes, so a change is seen straight away)."""
    store = _memo.get()
    if store is not None:
        store.clear()
